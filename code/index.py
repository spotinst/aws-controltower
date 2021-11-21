import boto3, logging, os, json
import time
from typing import Union
import urllib.request
from botocore.exceptions import ClientError
from enum import Enum
import random, string
import http.client
from http import HTTPStatus


LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

# constants
SPOT_API_URL = "https://api.spotinst.io"
SPOT_TOKEN_PREFIX = "Bearer "
CREATE_SPOT_USER_URI = SPOT_API_URL + "/setup/account"
SET_AWS_CREDENTIALS_URI = SPOT_API_URL + "/setup/credentials/aws"
GENERATE_AWS_CREDENTIALS_URL = SPOT_API_URL + "/setup/credentials/aws/externalId"
SPOT_TEMPLATE_URL = "https://s3.amazonaws.com/spotinst-public/assets/cloudformation/templates/onboarding/spotinst_aws_cfn_account_credentials_stack.template.json"

# stack constants
PRINCIPAL = "arn:aws:iam::922761411349:root"
STACK_TOKEN = "SomeToken"
EXT_ID_PREFIX = "spotinst:aws:extid:"


# helper classes
class StackInstanceOperationStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    INOPERABLE = "INOPERABLE"


class CloudWatchEventName(str, Enum):
    CREATED = "CreateManagedAccount"
    UPDATED = "UpdateManagedAccount"


class AWSAccountCreationState(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class UnexpectedResponseStructureException(Exception):
    pass


class SpotAPIException(Exception):
    pass


class UndesiredInstanceStatusException(Exception):
    pass


# handler
def lambda_handler(event, context):
    client = boto3.client("cloudformation")
    stackset_name = "SpotAWSControlTowerStackSet"
    # generate external random ID here
    external_id = EXT_ID_PREFIX + generate_random_alphanum(16)
    regionName = context.invoked_function_arn.split(":")[3]
    # are we sure this is going to point us to the right main account # on AWS?
    mainAccountId = context.invoked_function_arn.split(":")[4]
    eventDetails = event["detail"]
    event_name = eventDetails["eventName"]
    is_created_event = event_name == CloudWatchEventName.CREATED

    if not is_created_event:
        LOGGER.debug(f"irrelevant event: {event_name}, skipping...")
        return

    serviceEventDetails = eventDetails["serviceEventDetails"]
    newAccountInfo = serviceEventDetails["createManagedAccountStatus"]
    commandState = newAccountInfo["state"]
    account_creation_succeeded = commandState == AWSAccountCreationState.SUCCEEDED

    if not account_creation_succeeded:
        LOGGER.warning(
            f"AWS Account creation did not succeed, status is: {commandState}, not proceeding..."
        )
        return

    aws_new_acct_id = newAccountInfo["account"]["accountId"]
    aws_new_acct_name = newAccountInfo["account"]["accountName"]

    try:
        LOGGER.debug("checking if stackset exists on main account")
        client.describe_stack_set(StackSetName=stackset_name)
    except client.exceptions.StackSetNotFoundException as e:
        LOGGER.info("Stack set does not exist on main account, creating it now.")
        response: dict = create_stack_set_in_main(
            client,
            stackset_name=stackset_name,
            external_id=external_id,
            principal=PRINCIPAL,
            stack_token=STACK_TOKEN,
            aws_main_account_id=mainAccountId,
        )

        stackset_id = response.get("StackSetId")

        LOGGER.info(
            f"successfully created stack set in main account, StackSetId is: {stackset_id}"
        )

        try:
            client.describe_stack_set(StackSetName=stackset_name)
        except client.exceptions.StackSetNotFoundException as desc_e:
            LOGGER.error("Failed to get , {}".format(desc_e))
            raise desc_e

    try:
        spot_auth_token = get_spot_auth_token()
    except KeyError:
        LOGGER.error(
            f"SpotToken environment variable missing, cannot proceed with Spot account creation"
        )
        return

    LOGGER.info(
        f"Attempting to create a new Spot account for AWS account: {aws_new_acct_name}"
    )

    try:
        new_spot_acct_id = create_spot_acct_get_id(
            spot_auth_token=spot_auth_token, account_name=aws_new_acct_name + f" (CT)"
        )
        gen_ext_id = spot_generate_external_id_for_account(spot_account_id=new_spot_acct_id,
                                                           spot_auth_token=spot_auth_token)
    except SpotAPIException as api_e:
        LOGGER.error(f"failed to create Spot account and generate external ID, error: {api_e}")
        return
    except UnexpectedResponseStructureException as res_e:
        LOGGER.error(f"failed to create Spot account and generate external ID, error: {res_e}")
        return

    try:
        account_list = []
        account_list.append(aws_new_acct_id)
        client.create_stack_instances(
            StackSetName=stackset_name,
            Accounts=account_list,
            Regions=[regionName],
            ParameterOverrides=[
                {
                    "ParameterKey": "AccountId",
                    "ParameterValue": new_spot_acct_id,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string",
                },
                {
                    "ParameterKey": "ExternalId",
                    "ParameterValue": gen_ext_id,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string",
                },
                {
                    "ParameterKey": "Principal",
                    "ParameterValue": PRINCIPAL,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string",
                },
                {
                    "ParameterKey": "Token",
                    "ParameterValue": spot_auth_token,
                    "UsePreviousValue": False,
                    "ResolvedValue": "string",
                },
            ],
        )
        # poll for instance status (see StackInstanceStatus)
        try:
            stack_instance_id = wait_for_stack_instance_success_status(
                client,
                instance_dict=None,
                stackset_name=stackset_name,
                stack_instance_account=aws_new_acct_id,
                stack_instance_region=regionName,
                num_retries=6,
                retry_duration_secs=60,
            )
        except UndesiredInstanceStatusException as e:
            LOGGER.error(
                f"newly created stack instance status is not {StackInstanceOperationStatus.SUCCEEDED}, error: {str(e)}"
            )
            return

        LOGGER.info(f"stack instance successfully created on accounts: {account_list}")
        LOGGER.info(
            f"attempting to assumeRole on {aws_new_acct_id} before getting stack-instance output"
        )
        # assumeRole on newly created AWS account
        assume_role_client = assume_role_on_aws_account(
            amazon_account_id=aws_new_acct_id, region_name=regionName
        )
        # get the stack instance output on the child account
        relevant_output = get_role_arn_from_instance_output(
            assume_role_client=assume_role_client,
            stack_id=stack_instance_id,
        )
        LOGGER.info(f"successfully obtained SpotinstRoleArn, value: {relevant_output}")
        # set AWS credentials on Spot
        try:
            did_credentials_succeed = spot_set_credentials_aws(
                spot_account_id=new_spot_acct_id,
                iam_role=relevant_output,
                spot_auth_token=spot_auth_token,
            )
            if did_credentials_succeed:
                LOGGER.info(f"successfully completed AWS -> Spot Account connection.")
                LOGGER.info(f"Done.")
            else:
                LOGGER.error("failed to set AWS credentials on Spot")
        except UnexpectedResponseStructureException as e:
            LOGGER.error(e)
            return

    except ClientError as creation_exp:
        LOGGER.error(f"Exception creating stack instance with {creation_exp}")
        raise creation_exp


# helper functions
def create_stack_set_in_main(
        client, *, stackset_name, external_id, principal, stack_token, aws_main_account_id
) -> dict:
    return client.create_stack_set(
        StackSetName=stackset_name,
        Description="Spot integration with AWS Control Tower CloudFormation Set",
        TemplateURL=SPOT_TEMPLATE_URL,
        Parameters=[
            {
                "ParameterKey": "AccountId",
                "ParameterValue": aws_main_account_id,
                "UsePreviousValue": False,
                "ResolvedValue": "string",
            },
            {
                "ParameterKey": "ExternalId",
                "ParameterValue": external_id,
                "UsePreviousValue": False,
                "ResolvedValue": "string",
            },
            {
                "ParameterKey": "Principal",
                "ParameterValue": principal,
                "UsePreviousValue": False,
                "ResolvedValue": "string",
            },
            {
                "ParameterKey": "Token",
                "ParameterValue": stack_token,
                "UsePreviousValue": False,
                "ResolvedValue": "string",
            },
        ],
        Capabilities=["CAPABILITY_NAMED_IAM"],
        AdministrationRoleARN="arn:aws:iam::"
                              + aws_main_account_id
                              + ":role/service-role/AWSControlTowerStackSetRole",
        ExecutionRoleName="AWSControlTowerExecution",
    )


def create_spot_acct_get_id(*, spot_auth_token: str, account_name: str) -> str:
    """
    Makes a request to create a new account under an organization in Spot.

    Arguments:
        spot_auth_token {str} -- valid Spot token
        account_name {str} -- the name the new account should have

    Returns:
        [str] -- the ID of newly created account
    """
    body = {"account": {"name": account_name}}
    enc_body = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(CREATE_SPOT_USER_URI, data=enc_body)
    req.add_header("content-type", "application/json")
    req.add_header("Authorization", f"{SPOT_TOKEN_PREFIX}{spot_auth_token}")
    response: http.client.HTTPResponse = urllib.request.urlopen(req)

    if response.status != HTTPStatus.OK:
        raise SpotAPIException(
            f"got status other than OK: {response.status}, response:\n {response}"
        )

    resp_as_json = json.load(response)
    inner_response = resp_as_json.get("response")

    if (
            not "response" in resp_as_json  # no inner response key in response
            or (not "items" in inner_response)  # not items in response
            or (not inner_response.get("items"))  # items is an empty list
    ):
        raise UnexpectedResponseStructureException(
            f"got unexpected response structure, response:\n {json.dumps(resp_as_json)}"
        )

    new_acct_id = resp_as_json.get("response").get("items")[0].get("id")
    LOGGER.info(f"Successfully created a new Spot account with name: {account_name}")
    return new_acct_id


def spot_set_credentials_aws(
        *, spot_account_id: str, iam_role: str, spot_auth_token: str
) -> bool:
    """
    Makes a request to set AWS credentials for a Spot account.

    Arguments:
        spot_account_id {str} -- the Spot account for which to set AWS credentials
        iam_role {str} -- the IAM role
        external_id {str} -- the external ID used while setting up the IAM role
        spot_auth_token {str} -- valid Spot token

    Returns:
        [bool] -- whether the operation succeeded
    """
    body = {"credentials": {"iamRole": iam_role}}
    query_param = f"?accountId={spot_account_id}"
    enc_body = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(SET_AWS_CREDENTIALS_URI + query_param, data=enc_body)
    req.add_header("content-type", "application/json")
    req.add_header("Authorization", f"{SPOT_TOKEN_PREFIX}{spot_auth_token}")
    response = urllib.request.urlopen(req)

    if response.status != HTTPStatus.OK:
        raise SpotAPIException(
            f"got status other than OK: {response.status}, response:\n {response}"
        )

    resp_as_json = json.load(response)
    inner_response = resp_as_json.get("response")

    if (
            not "response" in resp_as_json
            or (not "status" in inner_response)
            or (not "code" in inner_response.get("status"))
    ):
        raise UnexpectedResponseStructureException(
            f"got unexpected response structure, response:\n {json.dumps(resp_as_json)}"
        )

    status_code = inner_response.get("status").get("code")

    if status_code == HTTPStatus.OK:
        LOGGER.info(
            f"successfully set AWS credentials for Spot account {spot_account_id}"
        )
        return True
    else:
        LOGGER.error(
            f"Failed to set AWS credentials for Spot account {spot_account_id}, status code: {status_code}, response:\n {inner_response}"
        )
        return False


def generate_random_alphanum(length: int) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def wait_for_stack_instance_success_status(
        client,
        *,
        instance_dict: Union[dict, None],
        stackset_name: str,
        stack_instance_account: str,
        stack_instance_region: str,
        num_retries: int,
        retry_duration_secs: int,
) -> str:

    wait_for_statuses = [
        StackInstanceOperationStatus.PENDING,
        StackInstanceOperationStatus.RUNNING,
    ]
    # on first run, instance_dict is None, later it's the latest dict object we got.
    status = None

    if isinstance(instance_dict, dict):
        status = (
            instance_dict.get("StackInstance")
                .get("StackInstanceStatus")
                .get("DetailedStatus")
        )

    if (status is None or status in wait_for_statuses) and num_retries > 0:
        LOGGER.debug(
            f"polling AWS for StackInstanceStatus, retries left: {num_retries}"
        )
        num_retries -= 1
        time.sleep(retry_duration_secs)
        resp = client.describe_stack_instance(
            StackSetName=stackset_name,
            StackInstanceAccount=stack_instance_account,
            StackInstanceRegion=stack_instance_region,
        )
        instance_dict = resp
        return wait_for_stack_instance_success_status(
            client=client,
            instance_dict=instance_dict,
            stackset_name=stackset_name,
            stack_instance_account=stack_instance_account,
            stack_instance_region=stack_instance_region,
            num_retries=num_retries,
            retry_duration_secs=retry_duration_secs,
        )
    elif status in wait_for_statuses and num_retries <= 0:
        raise UndesiredInstanceStatusException(
            f"failure: stack instance status still showing {status} after {num_retries * retry_duration_secs} seconds have passed"
        )
    elif status == StackInstanceOperationStatus.SUCCEEDED:
        stack_instance_id = instance_dict.get("StackInstance").get("StackId")
        LOGGER.info(
            f"success: stack instance (ID: {stack_instance_id}) status is {status}"
        )
        return stack_instance_id
    else:
        raise UndesiredInstanceStatusException(f"instance status returned {status}")


def get_role_arn_from_instance_output(
        *, assume_role_client: boto3.client, stack_id: str
) -> str:
    """
    Calls describe_stacks, returns SpotinstRoleArn from outputs

    Arguments:
        assume_role_client {str} -- a boto3.client
        stack_id {str} -- the ID of the stack

    Returns:
        [boto3.client] -- SpotinstRoleArn OutputValue
    """
    LOGGER.info(f"Calling describe stacks on stack ID: {stack_id}")
    # StackName can also be its ID
    stacks: dict = assume_role_client.describe_stacks(StackName=stack_id)
    LOGGER.info(f"attempting to parse outputs on stack ID: {stack_id}")
    outputs: list = stacks.get("Stacks")[0].get("Outputs")
    filtered_outputs = [x for x in outputs if x.get("OutputKey") == "SpotinstRoleArn"]
    relevant_output: str = filtered_outputs[0].get("OutputValue")
    return relevant_output


def assume_role_on_aws_account(
        *, amazon_account_id: str, region_name: str
) -> boto3.client:
    """
    Assumes role on a different AWS account and returns a boto3 client
    with proper credentials.

    Arguments:
        amazon_account_id {str} -- the Amazon account to assume the role on
        region_name {str} -- the region name for the account to assume role on

    Returns:
        [boto3.client] -- a boto client.
    """
    boto_sts = boto3.client("sts")
    sts_response = boto_sts.assume_role(
        RoleArn=f"arn:aws:iam::{amazon_account_id}:role/AWSControlTowerExecution",
        RoleSessionName="SpotLambdaSession",
    )
    new_session_id = sts_response["Credentials"]["AccessKeyId"]
    new_session_key = sts_response["Credentials"]["SecretAccessKey"]
    new_session_token = sts_response["Credentials"]["SessionToken"]
    assume_role_client = boto3.client(
        "cloudformation",
        region_name=region_name,
        aws_access_key_id=new_session_id,
        aws_secret_access_key=new_session_key,
        aws_session_token=new_session_token,
    )
    return assume_role_client


def get_spot_auth_token():
    ssm = boto3.client("ssm")
    parameter = ssm.get_parameter(Name="spot-auth-token", WithDecryption=True)
    return parameter["Parameter"]["Value"]


def spot_generate_external_id_for_account(*, spot_account_id: str, spot_auth_token: str) -> str:
    """
    Generate an external ID for spot_account_id to be used in the set_credentials flow.

    :param spot_account_id: the Spot account ID that the ID will be generated for
    :param spot_auth_token: a valid Spot auth token
    :return: None
    """
    query_param = f"?accountId={spot_account_id}"
    req = urllib.request.Request(url=GENERATE_AWS_CREDENTIALS_URL + query_param, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("Authorization", f"{SPOT_TOKEN_PREFIX}{spot_auth_token}")
    response = urllib.request.urlopen(req)

    if response.status != HTTPStatus.OK:
        raise SpotAPIException(
            f"got status other than OK: {response.status}, response:\n {response}"
        )

    resp_as_json = json.load(response)
    inner_response = resp_as_json.get("response")

    if (
            not "response" in resp_as_json
            or (not "status" in inner_response)
            or (not "code" in inner_response.get("status"))
    ):
        raise UnexpectedResponseStructureException(
            f"got unexpected response structure, response:\n {json.dumps(resp_as_json)}"
        )

    status_code = inner_response.get("status").get("code")

    if status_code == HTTPStatus.OK:
        LOGGER.info(
            f"successfully generated AWS credentials for Spot account {spot_account_id}"
        )
        ext_id = resp_as_json.get("response").get("items")[0].get("externalId")
        LOGGER.info(f"Successfully created new external ID: {ext_id}")
        return ext_id
    else:
        msg = f"Failed to set AWS credentials for Spot account {spot_account_id}, status code: {status_code}, response:\n {inner_response}"
        LOGGER.error(msg)
        raise SpotAPIException(msg)
