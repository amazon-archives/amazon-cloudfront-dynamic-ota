import boto3
import argparse
import os
import hashlib
import base64

parser = argparse.ArgumentParser()
parser.add_argument('cfn_stack_name')
parser.add_argument('--create', action='store_true')
parser.add_argument('--profile', default=None)
parser.add_argument('--compute', default='EdgeLambda')
parser.add_argument('--source', default='https://github.com/aws-samples/amazon-cloudfront-dynamic-ota')
parser.add_argument('--sourceauth', default='None')
args = parser.parse_args()

if args.profile:
    boto3.setup_default_session(profile_name=args.profile)
cloudformation = boto3.client('cloudformation')
dynamodb = boto3.resource('dynamodb')
s3 = boto3.resource('s3')

if args.create:
    with open('infrastructure/cloudformation_template.json') as template_file_obj:
        cfn_template = template_file_obj.read()

    template_params = [
        {
            'ParameterKey': 'ComputeType',
            'ParameterValue': args.compute
        },
        {
            'ParameterKey': 'VPCCIDRPrefix',
            'ParameterValue': '172.31'
        },
        {
            'ParameterKey': 'ProjectSource',
            'ParameterValue': args.source
        },
        {
            'ParameterKey': 'SourceConnectionArn',
            'ParameterValue': args.sourceauth
        }
    ]


    stack_create_params = {
        'StackName': args.cfn_stack_name,
        'TemplateBody': cfn_template,
        'Parameters': template_params,
        'Capabilities': ['CAPABILITY_IAM']
    }

    create_result = cloudformation.create_stack(**stack_create_params)
    waiter = cloudformation.get_waiter('stack_create_complete')
    print("...waiting for stack to be ready...")
    waiter.wait(StackName=args.cfn_stack_name)

dynamo_items = [
    {
        "app": "os_armv8",
        "env": "beta",
        "version": "2.0.0",
        "ident": "alx_2.0.0",
        "cpuArch": "armv8",
    },
    {
        "app": "os_armv8",
        "env": "prod",
        "version": "1.1.0",
        "ident": "alx_1.1.0",
        "cpuArch": "armv8",
    },
    {
        "app": "os_armv7",
        "env": "beta",
        "version": "2.0.0",
        "ident": "als_2.0.0",
        "cpuArch": "armv7",
    },
    {
        "app": "os_armv7",
        "env": "prod",
        "version": "1.0.0",
        "ident": "als_1.0.0",
        "cpuArch": "armv7",
    },
    {
        "app": "scoreboard",
        "env": "beta",
        "version": "2.0.0",
        "deviceAttr": {
        "gamer": True
        },
        "ident": "ab_2.0.0",
    },
    {
        "app": "scoreboard",
        "env": "prod",
        "version": "1.0.0",
        "deviceAttr": {
        "gamer": True
        },
        "ident": "ab_1.0.0",
    },
    {
        "app": "videoStreamer",
        "env": "beta",
        "version": "0.1.2",
        "deviceAttr": {
        "camera": True
        },
        "ident": "a_0.1.2",
    },
    {
        "app": "videoStreamer",
        "env": "prod",
        "version": "0.0.1",
        "deviceAttr": {
        "camera": True,
        "gamer": True
        },
        "ident": "a_0.0.1",
    },
    {
        "app": "modemFW",
        "env": "prod",
        "version": "1.0",
        "deviceAttr": {
        "cellular": True
        },
        "ident": "b_1.0",
    }
]


resource_summaries = cloudformation.list_stack_resources(StackName=args.cfn_stack_name).get('StackResourceSummaries')

if not resource_summaries:
    print("No stack found")

for r in resource_summaries:
    if r['LogicalResourceId'] == 'AppVersionsTable':
        dynamo_table = dynamodb.Table(r['PhysicalResourceId'])
    elif r['LogicalResourceId'] == 'AppBinaries':
        s3_bucket = s3.Bucket(r['PhysicalResourceId'])

for i in dynamo_items:
    s3_obj = os.urandom(10485760)
    s3_obj_hash = hashlib.md5(s3_obj).digest()
    dynamo_obj_hash = hashlib.md5(s3_obj).hexdigest()
    content_md5 = base64.b64encode(s3_obj_hash).decode('utf-8')
    s3_key = '{0}_{1}'.format(i.get('app'), i.get('version'))
    s3_bucket.put_object(
        Body=s3_obj,
        Key=s3_key,
        ContentMD5=content_md5
    )
    i['url'] = "s3://{0}/{1}".format(s3_bucket.name, s3_key)
    i['md5'] = dynamo_obj_hash
    dynamo_table.put_item(Item=i)

