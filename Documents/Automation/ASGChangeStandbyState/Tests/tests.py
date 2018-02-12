import ConfigParser
import logging
import os
import sys
import time
import unittest
import json

import boto3

DOC_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
REPO_ROOT = os.path.dirname(DOC_DIR)

# Import shared testing code
sys.path.append(
    os.path.join(
        REPO_ROOT,
        'Testing'
    )
)
import ssm_testing  # noqa pylint: disable=import-error,wrong-import-position

CONFIG = ConfigParser.ConfigParser()
CONFIG.readfp(open(os.path.join(REPO_ROOT, 'Testing', 'defaults.cfg')))
CONFIG.read([os.path.join(REPO_ROOT, 'Testing', 'local.cfg')])

REGION = CONFIG.get('general', 'region')
PREFIX = CONFIG.get('general', 'resource_prefix')
SERVICE_ROLE_NAME = CONFIG.get('general', 'automation_service_role_name')

AMI_ID = CONFIG.get('windows', 'windows2016.{}'.format(REGION))
INSTANCE_TYPE = CONFIG.get('windows', 'instance_type')

ENTER_STANDBY_SSM_DOC_NAME = PREFIX + 'automation-asg-enter-standby'
ENTER_STANDBY_CFN_STACK_NAME = PREFIX + 'automation-asg-enter-standby'

EXIT_STANDBY_SSM_DOC_NAME = PREFIX + 'automation-asg-exit-standby'
EXIT_STANDBY_CFN_STACK_NAME = PREFIX + 'automation-asg-exit-standby'

logging.basicConfig(level=CONFIG.get('general', 'log_level').upper())
LOGGER = logging.getLogger(__name__)
logging.getLogger('botocore').setLevel(level=logging.WARNING)

vpcUtil = ssm_testing.VPCTester(boto3.resource('ec2', region_name=REGION))

ec2_client = boto3.client('ec2', region_name=REGION)
as_client = boto3.client('autoscaling', region_name=REGION)
cfn_client = boto3.client('cloudformation', region_name=REGION)
ssm_client = boto3.client('ssm', region_name=REGION)
sts_client = boto3.client('sts', region_name=REGION)
iam_client = boto3.client('iam', region_name=REGION)


class create_admin_role:
    def __init__(self, iam_client, sts_client, role_name, user_arn):
        self.user_arn = user_arn
        self.role_name = role_name
        self.iam_client = iam_client
        self.sts_client = sts_client

    def __enter__(self):
        self.cleanup()
        assume_role = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": [
                            "lambda.amazonaws.com",
                            "ssm.amazonaws.com",
                            "cloudformation.amazonaws.com",
                            "ec2.amazonaws.com"
                        ]
                    },
                    "Action": "sts:AssumeRole"
                },
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": self.user_arn},
                    "Action": "sts:AssumeRole"
                }
            ]
        }
        result = self.iam_client.create_role(RoleName=self.role_name, AssumeRolePolicyDocument=json.dumps(assume_role))
        self.iam_client.attach_role_policy(RoleName=self.role_name, PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess")

        # For what ever reason assuming a role that got created too fast fails, so we just wait until we can.
        retry_count = 6
        while True:
            try:
                self.sts_client.assume_role(RoleArn=result["Role"]["Arn"], RoleSessionName="checking_assume")
                break
            except Exception as e:
                retry_count -= 1
                if retry_count == 0:
                    raise e

                LOGGER.info("Unable to assume role... trying again in 10 sec")
                time.sleep(10)

        return result

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def cleanup(self):
        try:
            self.iam_client.detach_role_policy(
                RoleName=self.role_name,
                PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess"
            )
        except Exception as e:
            pass

        try:
            self.iam_client.delete_role(RoleName=self.role_name)
        except Exception as e:
            pass


class TestCase(unittest.TestCase):
    def test_enter_standby_document(self):
        user_arn = boto3.client('sts', region_name=REGION).get_caller_identity().get('Arn')
        with create_admin_role(iam_client, sts_client, PREFIX + "enter-standby-test", user_arn) as admin_role:
            admin_role_arn = admin_role["Role"]["Arn"]
            available_subnets = vpcUtil.find_default_subnets()

            # ensure the account being used has a default VPC.
            assert len(available_subnets) > 0, "No default subnet available for testing"

            ssm_doc = ssm_testing.SSMTester(
                ssm_client=ssm_client,
                doc_filename=os.path.join(
                    DOC_DIR,
                    'Output/aws-ASGEnterStandby.json'),
                doc_name=ENTER_STANDBY_SSM_DOC_NAME,
                doc_type='Automation'
            )

            test_cf_stack = ssm_testing.CFNTester(
                cfn_client=cfn_client,
                template_filename=os.path.abspath(os.path.join(
                    DOC_DIR,
                    'Tests/CloudFormationTemplates/ASG.yml')),
                stack_name=ENTER_STANDBY_CFN_STACK_NAME
            )

            LOGGER.info('Creating AutoScaling Group for testing')
            stack_param = {
                'AMI': AMI_ID,
                'Subnets': available_subnets[0].id,
                'InstanceType': INSTANCE_TYPE
            }
            test_cf_stack.create_stack([
                {'ParameterKey': key, 'ParameterValue': value} for key, value in stack_param.iteritems()])

            asg_name = test_cf_stack.stack_outputs["ASGName"]
            LOGGER.info("CF Stack Outputs: " + str(test_cf_stack.stack_outputs))

            LOGGER.info("Waiting for an instance to become ready...")
            working_instance = asg_wait_for_running_instance(
                asg_name=asg_name,
                number_of_instance=1,
                max_wait_sec=1800)[0]

            try:

                LOGGER.info("Creating automation document")
                assert ssm_doc.create_document() == 'Active', 'Document not created successfully'

                LOGGER.info(
                    "Executing SSM automation document to set instance to standby mode on {}".format(working_instance))

                execution = ssm_doc.execute_automation(
                    params={'LambdaRoleArn': [admin_role_arn],
                            'InstanceId': [working_instance],
                            'AutomationAssumeRole': [admin_role_arn]})

                # Collect asg instance lifecycle change.

                asg_status_changes = []
                asg_status_ignores = ["EnteringStandby", "Pending"]

                # Status callback to collect any necessary data
                def status_callback(_):
                    collect_asg_status_change(asg_name, working_instance, asg_status_ignores, asg_status_changes)

                # Wait for SSM to finish while collecting value change (callback).
                result = ssm_doc.automation_execution_status(ssm_client, execution, status_callback=status_callback)

                # Verify instance status change.
                LOGGER.info("ASG status change sequence: " + str(asg_status_changes))
                expected_status_change_sequence = [
                    "InService",
                    "Standby"
                ]
                is_status_change_expected = asg_status_changes == expected_status_change_sequence
                assert is_status_change_expected, 'ASG instant lifecycle did not match expected.'

                LOGGER.info('Verifying automation executions have concluded successfully')
                assert result == 'Success', 'Document did not complete'

            finally:
                try:
                    LOGGER.info('Taking instance out of standby (required for CF stack teardown)')
                    as_client.exit_standby(
                        InstanceIds=[working_instance],
                        AutoScalingGroupName=asg_name)

                finally:
                    test_cf_stack.delete_stack()
                    ssm_doc.destroy()

    def test_exit_standby_document(self):
        user_arn = boto3.client('sts', region_name=REGION).get_caller_identity().get('Arn')
        with create_admin_role(iam_client, sts_client, PREFIX + "exit-standby-test", user_arn) as admin_role:
            admin_role_arn = admin_role["Role"]["Arn"]

            available_subnets = vpcUtil.find_default_subnets()

            # ensure the account being used has a default VPC.
            assert len(available_subnets) > 0, "No default subnet available for testing"

            ssm_doc = ssm_testing.SSMTester(
                ssm_client=ssm_client,
                doc_filename=os.path.join(
                    DOC_DIR,
                    'Output/aws-ASGExitStandby.json'),
                doc_name=EXIT_STANDBY_SSM_DOC_NAME,
                doc_type='Automation'
            )

            test_cf_stack = ssm_testing.CFNTester(
                cfn_client=cfn_client,
                template_filename=os.path.abspath(os.path.join(
                    DOC_DIR,
                    'Tests/CloudFormationTemplates/ASG.yml')),
                stack_name=EXIT_STANDBY_CFN_STACK_NAME
            )

            LOGGER.info('Creating AutoScaling Group for testing')
            stack_param = {
                'AMI': AMI_ID,
                'Subnets': available_subnets[0].id,
                'InstanceType': INSTANCE_TYPE
            }
            test_cf_stack.create_stack([
                {'ParameterKey': key, 'ParameterValue': value} for key, value in stack_param.iteritems()])

            asg_name = test_cf_stack.stack_outputs["ASGName"]

            LOGGER.info("Waiting for an instance to become ready...")
            working_instance = asg_wait_for_running_instance(
                asg_name=asg_name,
                number_of_instance=1,
                max_wait_sec=1800)[0]

            LOGGER.info("Setting instance to enter standby mode")
            as_client.enter_standby(
                InstanceIds=[working_instance],
                AutoScalingGroupName=asg_name,
                ShouldDecrementDesiredCapacity=True)

            # poll the instance until it reaches the standby state
            asg_wait_for_instance_in_state(working_instance, 'Standby')

            try:

                LOGGER.info("Creating automation document")
                assert ssm_doc.create_document() == 'Active', 'Document not created successfully'

                LOGGER.info(
                    "Executing SSM automation document to remove instance from standby mode on {}".format(working_instance))
                execution = ssm_doc.execute_automation(
                    params={'LambdaRoleArn': [admin_role_arn],
                            'InstanceId': [working_instance],
                            'AutomationAssumeRole': [admin_role_arn]})

                # Collect asg instance lifecycle change.

                asg_status_changes = []
                asg_status_ignores = ["Pending"]

                # Status callback to collect any necessary data
                def status_callback(_):
                    collect_asg_status_change(asg_name, working_instance, asg_status_ignores, asg_status_changes)

                # Wait for SSM to finish while collecting value change (callback).
                result = ssm_doc.automation_execution_status(ssm_client, execution, status_callback=status_callback)

                # Verify instance status change.
                LOGGER.info("ASG status change sequence: " + str(asg_status_changes))
                expected_status_change_sequence = [
                    "Standby",
                    "InService"
                ]
                is_status_change_expected = asg_status_changes == expected_status_change_sequence
                assert is_status_change_expected, 'ASG instant lifecycle did not match expected.'

                LOGGER.info('Verifying automation executions have concluded successfully')
                assert result == 'Success', 'Document did not complete'

            finally:
                test_cf_stack.delete_stack()
                ssm_doc.destroy()


def asg_wait_for_instance_in_state(instance_id, desired_state, max_wait_sec=60):
    current_state = None
    sleep_counter = 0
    while current_state != desired_state and sleep_counter * 5 < max_wait_sec:
        current_state = as_client.describe_auto_scaling_instances(
            InstanceIds=[instance_id],
            MaxRecords=1)['AutoScalingInstances'][0]['LifecycleState']
        LOGGER.info("Current state of Instance {}: {}".format(instance_id, current_state))
        time.sleep(5)
        sleep_counter += 1


def asg_wait_for_running_instance(asg_name, number_of_instance, max_wait_sec=60):
    """
    Wait for ASG to start up some instance and return the instance id.

    :param asg_name: AutoScaling group's name.
    :param number_of_instance: Max number of instance to return.
    :param max_wait_sec: Number of sec, this function should wait for an instance.
    :return: list of instance id
    """
    found_instances = []
    sleep_counter = 0

    while True:
        asg_lists = as_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        assert len(asg_lists["AutoScalingGroups"]) > 0, "No AutoScaling Group found"

        instances = asg_lists["AutoScalingGroups"][0]["Instances"]
        if len(instances) > 0:
            describe_res = ec2_client.describe_instance_status(
                InstanceIds=[x["InstanceId"] for x in instances],
                IncludeAllInstances=True
            )

            for d in describe_res['InstanceStatuses']:
                instance_id = d["InstanceId"]
                if d['InstanceStatus']['Status'] == 'ok' and instance_id not in found_instances:
                    found_instances.append(d["InstanceId"])

                    if len(found_instances) == number_of_instance:
                        return found_instances

        assert sleep_counter * 10 < max_wait_sec, "Unable to find running instance"

        sleep_counter += 1
        time.sleep(10)


def collect_asg_status_change(name, instance_id, ignores, test_result):
    """
    Monitor Auto Scaling Group Instance lifecycle status and append changes to test_result.

    :param name: Auto Scaling Group name to monitor.
    :param instance_id:  Instance id to monitor.
    :param ignores: List of state to ignore.
    :param test_result: Location to append result.
    """
    describe_res = as_client.describe_auto_scaling_groups(
        AutoScalingGroupNames=[name]
    )["AutoScalingGroups"][0]

    for instance in describe_res["Instances"]:
        if instance["InstanceId"] == instance_id:
            state = instance["LifecycleState"]
            if state in ignores:
                continue

            if len(test_result) > 0:
                if test_result[-1] != state:
                    LOGGER.info("ASG Change Detection: {}".format(state))
                    test_result.append(state)
            else:
                LOGGER.info("ASG Change Detection: {}".format(state))
                test_result.append(state)
