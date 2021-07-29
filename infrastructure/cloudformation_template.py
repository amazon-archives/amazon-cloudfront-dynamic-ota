from troposphere import GetAtt, Join, Output, Parameter, Ref, Template,If, Equals, Sub, FindInMap
from troposphere.cloudfront import (
    CustomOriginConfig,
    DefaultCacheBehavior,
    Distribution,
    DistributionConfig,
    Function,
    Origin,
    OriginRequestCookiesConfig,
    ParametersInCacheKeyAndForwardedToOrigin,
    S3OriginConfig,
    LambdaFunctionAssociation,
    OriginRequestPolicy,
    OriginRequestPolicyConfig,
    OriginRequestHeadersConfig,
    OriginRequestQueryStringsConfig,
    CachePolicy,
    CachePolicyConfig,
    CacheHeadersConfig,
    CacheCookiesConfig,
    CacheQueryStringsConfig
)
from troposphere.iam import Role, Policy
from troposphere.ec2 import (
    InternetGateway, 
    VPCGatewayAttachment, 
    SubnetRouteTableAssociation, 
    Subnet, 
    RouteTable, 
    Route, 
    VPC, 
    EIP, 
    NatGateway
)
from troposphere.awslambda import Code, Function, Version
from troposphere.dynamodb import Table, KeySchema, AttributeDefinition
from troposphere.s3 import Bucket
from troposphere.apprunner import (
    AuthenticationConfiguration,
    CodeConfigurationValues,
    InstanceConfiguration, 
    Service as ARService,
    SourceCodeVersion, 
    SourceConfiguration, 
    CodeRepository, 
    CodeConfiguration, 
    KeyValuePair
)
from troposphere.ssm import Parameter as SSMParameter

ref_region = Ref('AWS::Region')
no_value = Ref("AWS::NoValue")


t = Template()

t.set_description(
    "CloudFormation template to create a demo of CloudFront OTA"
)


# Parameters

compute_type_param = t.add_parameter(
    Parameter(
        "ComputeType",
        Description="Compute type to be used for the application layer. This can be either Lambda@Edge or AppRunner (container)",
        Type="String",
        AllowedValues=["EdgeLambda", "AppRunner"],
        Default="EdgeLambda"
    )
)

vpc_cidr_prefix = t.add_parameter(Parameter(
    "VPCCIDRPrefix",
    Description="IP Address range for the VPN connected VPC",
    Default="172.31",
    Type="String",
))

project_source_param = t.add_parameter(Parameter(
    "ProjectSource",
    Type="String",
    Description="Demo Project Source. Don't change unless you're using a clone/fork of the original project repo",
    Default="https://github.com/aws-samples/amazon-cloudfront-dynamic-ota"
))

source_connection_arn = t.add_parameter(Parameter(
    "SourceConnectionArn",
    Type="String",
    Description="GitHub connection ARN to authenticate to private repos for App Runner deployments",
    Default="None"
))

# Conditions


t.add_condition(
    "UseEdgeLambda",
    Equals(
        Ref(compute_type_param),
        "EdgeLambda"
))


t.add_condition(
    "UseAppRunner",
    Equals(
        Ref(compute_type_param),
        "AppRunner"
        ))

# Mappings
t.add_mapping(
    "RoleTrustPolicyMap",
    {
        "AppRunner": {"roleTrust": ["tasks.apprunner.amazonaws.com"]},
        "EdgeLambda": {"roleTrust": ["edgelambda.amazonaws.com", "lambda.amazonaws.com"]}
    }
)

# Resources

## Application Resources



app_versions_table = t.add_resource(
    Table(
        "AppVersionsTable",
        AttributeDefinitions=[
            AttributeDefinition(AttributeName="app", AttributeType="S"),
            AttributeDefinition(AttributeName="env", AttributeType="S")
        ],
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            KeySchema(AttributeName="app", KeyType="HASH"),
            KeySchema(AttributeName="env", KeyType="RANGE")
        ],
    )
)

app_binary_bucket = t.add_resource(
    Bucket(
        "AppBinaries",
    )
)

ssm_param_dynamo_table = t.add_resource(
    SSMParameter(
        "DynamoTableNameSSMParam",
        Name="/cf-ota-lambda/APP_LOOKUP_TABLE",
        Type="String",
        Value=Ref(app_versions_table),
        Condition="UseEdgeLambda"
    )
)

ssm_param_s3_bucket = t.add_resource(
    SSMParameter(
        "S3BucketNameSSMParam",
        Name="/cf-ota-lambda/APP_BINARIES_BUCKET",
        Type="String",
        Value=Ref(app_binary_bucket),
        Condition="UseEdgeLambda"
    )
)

app_execution_role = t.add_resource(
    Role(
        "AppExecutionRole",
        Path="/",
        Policies=[
            Policy(
                PolicyName="logs",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Action": ["logs:*"],
                            "Resource": "arn:aws:logs:*:*:*",
                            "Effect": "Allow",
                        }
                    ],
                },
            ),
            Policy(
                PolicyName="s3",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Action": ["s3:GetObject"],
                            "Resource": [
                                Sub("${AppBinaries.Arn}/*")
                                ],
                            "Effect": "Allow",
                        }
                    ],
                },
            ),
            Policy(
                PolicyName="dynamo",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Action": ["dynamodb:PartiQLSelect"],
                            "Resource": [
                                Sub("${AppVersionsTable.Arn}")
                                ],
                            "Effect": "Allow",
                        }
                    ],
                },
            ),
            If("UseEdgeLambda", Policy(
                PolicyName="ssm",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Action": ["ssm:GetParameter"],
                            "Resource": [
                                Sub("arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/cf-ota-lambda/*")
                                ],
                            "Effect": "Allow",
                        }
                    ],
                },
            ),
            no_value)
        ],
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": ["sts:AssumeRole"],
                    "Effect": "Allow",
                    "Principal": {"Service": FindInMap("RoleTrustPolicyMap", Ref(compute_type_param), "roleTrust")},
                }
            ],
        },
    )
)




app_lambda = t.add_resource(
    Function(
        "AppEdgeLambda",
        Code=Code(
            S3Bucket="aws-iot-samples-artifacts",
            S3Key="cf-iot-ota-app.zip"
        ),
        Handler="app.edgelambda_handler",
        Role=GetAtt(app_execution_role, "Arn"),
        Runtime="python3.8",
        MemorySize=256,
        Timeout=30,
        Condition="UseEdgeLambda"
    )
)

app_lambda_version = t.add_resource(
    Version(
        "AppLambdaVersion",
        FunctionName=Ref(app_lambda),
        Condition="UseEdgeLambda"
    ),
)
source_configuration = SourceConfiguration(
            AuthenticationConfiguration=AuthenticationConfiguration(
                ConnectionArn=Ref(source_connection_arn)
                ),
            AutoDeploymentsEnabled=True,
            CodeRepository=CodeRepository(
                RepositoryUrl=Ref(project_source_param),
                SourceCodeVersion=SourceCodeVersion(
                    Type="BRANCH",
                    Value="main"
                ),
                CodeConfiguration=CodeConfiguration(
                    ConfigurationSource="API",
                    CodeConfigurationValues=CodeConfigurationValues(
                        BuildCommand="pip install -r runtime/requirements.txt",
                        Port="5000",
                        Runtime="PYTHON_3",
                        StartCommand="python runtime/app.py",
                        RuntimeEnvironmentVariables=[
                            KeyValuePair(
                                Name="APP_LOOKUP_TABLE",
                                Value=Ref(app_versions_table)
                            ),
                            KeyValuePair(
                                Name="APP_BINARIES_BUCKET",
                                Value=Ref(app_binary_bucket)
                            )
                        ]
                    )
                )
            )
        )

app_apprunner_service = t.add_resource(
    ARService(
        "AppService",
        Condition="UseAppRunner",
        ServiceName="CF-IoT-OTA",
        SourceConfiguration=source_configuration,
        InstanceConfiguration=InstanceConfiguration(
            InstanceRoleArn=GetAtt(app_execution_role, "Arn")
        )
    )
)


ota_cf_origin_request_policy = t.add_resource(
    OriginRequestPolicy(
        "IoTOTAOrigin",
        OriginRequestPolicyConfig=OriginRequestPolicyConfig(
           Name="IoTOTAOrigin",
           CookiesConfig=OriginRequestCookiesConfig(
               CookieBehavior="none"
           ),
           HeadersConfig=OriginRequestHeadersConfig(
               HeaderBehavior="whitelist",
               Headers=["If-None-Match"]
           ),
           QueryStringsConfig=OriginRequestQueryStringsConfig(
               QueryStringBehavior="all"
           )
        )
    )
)

ota_cf_cache_policy = t.add_resource(
    CachePolicy(
        "IoTOTACachePolicy",
        CachePolicyConfig=CachePolicyConfig(
            Name="IoTOTACachePolicy",
            DefaultTTL=30,
            MaxTTL=100,
            MinTTL=1,
            ParametersInCacheKeyAndForwardedToOrigin=ParametersInCacheKeyAndForwardedToOrigin(
                CookiesConfig=CacheCookiesConfig(
                    CookieBehavior="none"
                ),
                EnableAcceptEncodingGzip=True,
                HeadersConfig=CacheHeadersConfig(
                    HeaderBehavior="whitelist",
                    Headers=["If-None-Match"],
                ),
                QueryStringsConfig=CacheQueryStringsConfig(
                    QueryStringBehavior="all"
                )
            )
        )
    )
)

cloudfront_distribution = t.add_resource(
    Distribution(
        "OTADistribution",
        DistributionConfig=DistributionConfig(
            Origins=[
                If("UseEdgeLambda", 
                    Origin(
                        Id="1",
                        DomainName="amazon.com",
                        CustomOriginConfig=CustomOriginConfig(
                            OriginProtocolPolicy="https-only"
                        )
                    ), 
                no_value),
                If("UseAppRunner", 
                    Origin(
                        Id="1",
                        DomainName=Sub("${AppService.ServiceUrl}"),
                        CustomOriginConfig=CustomOriginConfig(
                            OriginProtocolPolicy="https-only"
                        )
                    ), 
                no_value)
            ],
            DefaultCacheBehavior=DefaultCacheBehavior(
                TargetOriginId="1",
                CachePolicyId=Ref(ota_cf_cache_policy),
                ViewerProtocolPolicy="allow-all",
                LambdaFunctionAssociations=If("UseEdgeLambda", [LambdaFunctionAssociation(
                    EventType="origin-request",
                    LambdaFunctionARN=Ref(app_lambda_version)
                )], no_value),
                CachedMethods=["HEAD", "GET"],
                AllowedMethods=["HEAD", "GET"],
                OriginRequestPolicyId=Ref(ota_cf_origin_request_policy),

            ),
            Enabled=True,
            HttpVersion="http2",

        ),
    )
)

t.add_output(
    [
        Output("DistributionId", Value=Ref(cloudfront_distribution)),
        Output(
            "DistributionName",
            Value=Join("", ["https://", GetAtt(cloudfront_distribution, "DomainName")]),
        ),
    ]
)

print(t.to_json())