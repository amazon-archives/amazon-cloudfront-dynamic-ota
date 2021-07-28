import json
from typing import final
from urllib.parse import parse_qs
import boto3
from boto3.dynamodb.types import TypeDeserializer
import tarfile
import re
import io
import os
from flask import Flask, Response, request

flapp = Flask(__name__)

dynamodb = boto3.client('dynamodb')
deserializer = TypeDeserializer()
s3 = boto3.resource('s3')
ssm = boto3.client('ssm')

def edgelambda_handler(event, _):
    print("Received")
    global binaries_bucket
    global dynamo_table_name

    binaries_bucket_name = ssm.get_parameter(Name='/cf-ota-lambda/APP_BINARIES_BUCKET')['Parameter']['Value']
    dynamo_table_name = ssm.get_parameter(Name='/cf-ota-lambda/APP_LOOKUP_TABLE')['Parameter']['Value']
    
    binaries_bucket = s3.Bucket(binaries_bucket_name)

    request = event['Records'][0]['cf']['request']
    query_string = request.get('querystring')
    if not query_string:
        return create_http_response('No query params provided', 400)

    params = {k.lower(): v[0].lower() for k, v in parse_qs(query_string).items()}
    headers = {v[0]['key']: v[0]['value'] for v in request['headers'].values()}

    response = package_handler(params, headers)

    reformatted_headers = dict()
    for k, v in response['headers'].items():
        reformatted_headers[k.lower()] = [
                {
                     'key': k,
                     'value': v
                 }
            ]
    new_response = dict()
    new_response['headers'] = reformatted_headers
    new_response['status'] = response['status_code']
    new_response['body'] = response['body']

    return new_response

@flapp.route("/package", methods=['GET'])
def flask_get_packages():

    global binaries_bucket
    s3_bucket_name = os.environ.get('APP_BINARIES_BUCKET')
    binaries_bucket = s3.Bucket(s3_bucket_name)

    global dynamo_table_name
    dynamo_table_name = os.environ.get("APP_LOOKUP_TABLE")

    request_querystring = request.query_string
    params = {k.decode('utf-8').lower(): v[0].decode('utf-8').lower() for k, v in parse_qs(request_querystring).items()}
    headers = request.headers
    response = package_handler(params, headers)


    return Response(response=response['body'], status=response['status_code'], headers=response['headers'])


# Load Balancer health check route
@flapp.route("/", methods=['GET'])
def health_check():
    return Response(response="Hello World", status=200)

def package_handler(params, headers):
    etags_raw = headers.get('If-None-Match', None)
    if etags_raw:
        etags = etags_raw.split(',')
    else:
        etags = etags_raw

    query_results, status_code, payload_type_param = find_matching_apps(params)

    if status_code != 200:
        body = query_results
        payload_type = payload_type_param
    else:
        body, status_code, payload_type = build_packages_payload(query_results, payload_type_param, etags)
    response = create_http_response(body, status_code, payload_type)

    return response
        

def find_matching_apps(params):

    valid_payload_types = ['fullpayload', 'metadataonly']

    cpu_arch = params.get('cpuarch')
    os_env = params.get('os', 'prod')
    payload_type_param = params.get("payloadtype", "fullpayload")
    
    # Lambda doesn't support return values greater than 1MB, so if a client requests a full payload while running in Lambda, return an error
    if payload_type_param == "fullpayload" and os.environ.get('AWS_EXECUTION_ENV', "NotLambda").startswith('AWS_Lambda'):
        return "'Full Payload' not available in this environment, please include the query param: payloadType=metadataOnly", 405, "json"

    if not cpu_arch:
        response = "Missing cpuArch query param", 400, "json"
    elif payload_type_param not in valid_payload_types:
        response = "Invalid payloadType param", 400, "json"
    else:
        partiql_statement = """SELECT url, app, version, md5 FROM "{0}" WHERE""".format(dynamo_table_name)
        partiql_statement += " (app = 'os_{0}' AND env = '{1}')".format(cpu_arch, os_env)
        params.pop('cpuarch', None)
        params.pop('payloadtype', None)
        params.pop('os', None)
        for k, v in params.items():
            if k.startswith('attr'):
                partiql_statement += " or (deviceAttr.{0} = true AND env = '{1}')".format(k[4:], v)
            else:
                partiql_statement += " or (app = '{0}' AND env = '{1}')".format(k, v)
        print("Querying Dynamo:")
        print(partiql_statement)
        raw_dynamo_response = dynamodb.execute_statement(
            Statement=partiql_statement
        ).get('Items')
        if raw_dynamo_response:
            response = raw_dynamo_response, 200, payload_type_param
        else:
            response = "No deployment package found", 404, "json"
    return response


def build_packages_payload(dynamo_response, payload_type, etags):

    print("Building payload")
    object_key_pattern = "s3://.+/(.+)"
    tar_object = io.BytesIO()
    package_metadata = dict()
    if payload_type == "fullpayload":
        with tarfile.open(fileobj=tar_object, mode='w:gz') as t:
            binary_count = 0
            for i in dynamo_response:
                item = {k: deserializer.deserialize(v) for k, v in i.items()}

                if etags and item.get('md5') in etags:
                    status_code = 304
                else:
                    status_code = 200
                    file_name = "{0}_{1}".format(item.get('app'), item.get('version'))

                    if item.get('url').startswith('s3://'):
                        s3_key = re.search(object_key_pattern, item.get('url')).group(1)
                        data = io.BytesIO()
                        binaries_bucket.download_fileobj(s3_key, data)
                        data_tarinfo = tarfile.TarInfo(name=file_name)
                        data.seek(0, 2)
                        data_tarinfo.size = data.tell()
                        data.seek(0)
                        t.addfile(tarinfo=data_tarinfo, fileobj=data)
                        binary_count += 1
                        
                    else:
                        # Could add support for generic HTTP URLs here
                        print("Unsupported URL type")
                
                package_metadata[item.get('app')] = {
                    'latestVersion': item.get('version'),
                    'url': item.get('url'),
                    'status_code': status_code
                } 
            metadata_obj = io.BytesIO()
            metadata_obj.write(json.dumps(package_metadata).encode('utf-8'))
            metadata_obj_tarinfo = tarfile.TarInfo(name="package_details.json")
            metadata_obj.seek(0, 2)
            metadata_obj_tarinfo.size = metadata_obj.tell()
            metadata_obj.seek(0)
            t.addfile(tarinfo=metadata_obj_tarinfo, fileobj=metadata_obj)

        if binary_count:        
            response = tar_object.getvalue(), 200, "tar"
        else:
            response = "Not Modified", 304, "json"

    elif payload_type == "metadataonly":
        item_count = 0
        for i in dynamo_response:
            item = {k: deserializer.deserialize(v) for k, v in i.items()}
            if etags and item.get('md5') in etags:
                status_code = 304
            else:
                item_count += 1
                status_code = 200

            file_name = "{0}_{1}".format(item.get('app'), item.get('version'))
            package_metadata[item.get('app')] = {
                'latestVersion': item.get('version'),
                'url': item.get('url'),
                'status_code': status_code
            }
        if item_count:
            response = json.dumps(package_metadata), 200, "json"
        else:
            response = "Not Modified", 304, "json"
    return response


def create_http_response(results, status_code, payload_type='json'):

    if status_code == 200:
        response = create_success_response(status_code, payload_type, results)
    else:
        response = create_error_response(status_code, results)

    return response

def create_success_response(status_code, payload_type, results):

    content_type_map = {
        'json': 'application/json',
        'tar': 'application/x-gzip'
    }

    payload_type_specific_headers = {
        'json': {},
        'tar': {
            'Content-Encoding': 'gzip',
            'Content-Disposition': 'attachment; filename="ota-package.tar.gz" '
        }
    }

    success_response = {
        'status_code': status_code,
        'headers': {
            'Cache-Control': 'max-age=100',
            'Content-Type': content_type_map[payload_type],
            **payload_type_specific_headers[payload_type]
        },
        'body': results
    }

    return success_response

def create_error_response(status_code, results):
    base_error_response = {
        'status_code': status_code,
        'headers': {
            'Cache-Control': 'max-age=100',
            'Content-Type': 'application/json'
        },
        'body': json.dumps({
            'error': results
        })
    }

    internal_server_error_response = {
        'status_code': 500,
        'headers': {
            'Cache-Control': 'max-age=1',
            "Content-Type": 'application/json'
        },
        'body': {
            'error': 'Internal Server Error'
        }
    }
    return base_error_response

if __name__ == "__main__":
    flapp.run(host='0.0.0.0')
