#!/usr/bin/python3

import requests
import random
import time
import calendar
import os
import argparse
import datetime

# Console arguments
parser = argparse.ArgumentParser(
    description="Estimate number of cars at given rectangles (latitude-longitude) on given timeframes"
)
parser.add_argument("-p", "--project_id", help="Project ID from UP42 Console", type=str, required=True)
parser.add_argument("-k", "--api_key", help="API Key from UP42 Console", type=str, required=True)
parser.add_argument("-c", "--coordinates", nargs="+",
                    help="List of latitude-longitude pairs, each representing 2 corners of a square. Sample: " +
                         "37.327035,-121.941054:37.323451,-121.940485", required=True)
parser.add_argument("-t", "--timeframes", nargs="+",
                    help="List of latitude-longitude pairs, each representing 2 corners of a square. Sample: " +
                         "2019-12-01:2020-02-28", required=True)
parser.add_argument("-v", "--verbose", help="Output more debug information", action='store_true')
parser.add_argument("--no_store", help="Disables saving of raw archives from UP42", action='store_false')
parser.add_argument("--workflow_name_prefix",
                    help="Workflow name prefix to be passed to UP42 console. Default: covid19_car_estimate_",
                    default="covid19_car_estimate_")
parser.add_argument("--no_cleanup", help="Keep workflow in UP42 project after script is done", action='store_false')

args = parser.parse_args()


DEBUG_LOGGING = args.verbose
PROJECT_ID = args.project_id
API_KEY = args.api_key
SAVE_ALL_JOB_RESULTS = args.no_store
BASE_URL = "https://api.up42.com"
BASE_WORKFLOW_NAME = args.workflow_name_prefix
CLEANUP_WORKFLOW = args.no_cleanup


# Process input polygon - validate and convert into UP42 input format.
POLYGONS = []
for coordinate_pair in args.coordinates:
    poly = coordinate_pair.split(":")
    if len(poly) != 2:
        print("Bad coordinate pair: "+coordinate_pair)
        exit()
    converted_poly = []
    for point in poly:
        pt = point.split(",")
        if len(pt) != 2:
            print("Bad coordinate pair: " + coordinate_pair)
            exit()
        pt[0] = float(pt[0])
        pt[1] = float(pt[1])
        converted_poly.append(pt)

    prepared_poly = [
        [converted_poly[0][1], converted_poly[0][0]],
        [converted_poly[1][1], converted_poly[0][0]],
        [converted_poly[1][1], converted_poly[1][0]],
        [converted_poly[0][1], converted_poly[1][0]],
        [converted_poly[0][1], converted_poly[0][0]]
    ]
    POLYGONS.append(prepared_poly)


# Date validation helper.
def _validate_date(date_text):
    try:
        datetime.datetime.strptime(date_text, '%Y-%m-%d')
        return True
    except ValueError:
        return False


# Process input timeframes - validate and convert into UP42 input format.
TIME_LIMITS = []
for timeframe in args.timeframes:
    tf = timeframe.split(":")
    if len(tf) != 2:
        print("Bad timeframe: "+timeframe)
        exit()

    if not _validate_date(tf[0]):
        print("Bad date: "+tf[0])
        exit()
    if not _validate_date(tf[1]):
        print("Bad date: "+tf[1])
        exit()
    TIME_LIMITS.append(
        tf[0]+"T00:00:00+00:00/"+tf[1]+"T23:59:59+00:00"
    )

# Random name for workflow to help determine which one is it in UI later.
randomized_name = BASE_WORKFLOW_NAME + (''.join(random.choice("0123456789abcdef") for i in range(16)))

# Storage folder name for all tarballs.
current_timestamp = calendar.timegm(time.gmtime())
folder = "raw_job_" + str(current_timestamp)

# Constant block names for automatic search.
data_block_name = 'oneatlas-pleiades-aoiclipped'
processing_block_name = 'sm_veh-detection'


# API INTEGRATION #


# Simple bearer auth implementation to reduce amount of external dependencies.
class BearerAuth(requests.auth.AuthBase):
    def __init__(self, client_id, client_secret, timeout=60):
        self.ts = time.time() - timeout * 2
        self.timeout = timeout
        self.client_id = client_id
        self.client_secret = client_secret

    def _get_token(self):
        return requests.post(BASE_URL + "/oauth/token",
                             auth=(self.client_id, self.client_secret),
                             headers={'Content-Type': 'application/x-www-form-urlencoded'},
                             data={'grant_type': 'client_credentials'}
                             ).json()['access_token']

    def __call__(self, r):
        if time.time() - self.ts > self.timeout:
            self.token = self._get_token()
            self.ts = time.time()
        r.headers["authorization"] = "Bearer " + self.token
        return r


# API Client abstraction
class ApiClient(object):
    def __init__(self, base_url, project_id, api_key):
        self.base_url = base_url
        self.project_id = project_id
        self.api_key = api_key
        self.bearer_auth = BearerAuth(project_id, api_key)

    def _get_query(self, url):
        return requests.get(self.base_url + url, auth=self.bearer_auth)

    def _post_query(self, url, data):
        return requests.post(self.base_url + url, auth=self.bearer_auth, json=data)

    def _put_query(self, url, data):
        return requests.put(self.base_url + url, auth=self.bearer_auth, json=data)

    def _delete_query(self, url):
        return requests.delete(self.base_url + url, auth=self.bearer_auth)

    def get_blocks(self):
        return self._get_query("/blocks").json()

    def create_workflow(self, name, description):
        return self._post_query("/projects/" + self.project_id + "/workflows", data={
            'name': name,
            'description': description
        }).json()

    # This will fill a chain of blocks, one by one, and create it.
    def set_workflow_tasks(self, workflow_id, task_list):
        tasks = []
        previous_name = None
        for task in task_list:
            tasks.append({
                "name": task["name"],
                "parentName": previous_name,
                "blockId": task["id"]
            })
            previous_name = task["name"]

        return self._post_query("/projects/" + self.project_id + "/workflows/" + workflow_id + "/tasks",
                                data=tasks).json()

    def delete_workflow(self, workflow_id):
        return self._delete_query("/projects/" + self.project_id + "/workflows/" + workflow_id)

    def get_job(self, job_id):
        return self._get_query("/projects/" + self.project_id + "/jobs/" + job_id).json()

    def get_job_output(self, job_id):
        return self._get_query("/projects/" + self.project_id + "/jobs/" + job_id + "/outputs/data-json").json()

    def run_job(self, workflow_id, name, is_dry, job_parameters):
        data = job_parameters.copy()
        if is_dry:
            data['config'] = {"mode": "DRY_RUN"}
        return self._post_query("/projects/" + self.project_id + "/workflows/" + workflow_id + "/jobs?name=" + name,
                                data).json()

    def get_job_tasks(self, job_id):
        return self._get_query("/projects/" + self.project_id + "/jobs/" + job_id + "/tasks").json()

    def get_task_signed_url(self, job_id, task_id):
        return self._get_query(
            "/projects/" + self.project_id + "/jobs/" + job_id + "/tasks/" + task_id + "/downloads/results").json()

    def dump_task_url(self, url, target_folder, target_name):
        try:
            os.mkdir(target_folder)
        except:
            # We already have a preexisting directory with this name.
            pass

        output_location = os.path.join(target_folder, target_name)
        content = requests.get(url).content
        with open(output_location, "wb") as f:
            f.write(content)


api_client = ApiClient(BASE_URL, PROJECT_ID, API_KEY)


# Specific method which will extract target blocks that will be used in workflow.
# data_block_name will be used as source satellite data
# processing_block_name will be used for vehicle detection
def extract_target_blocks():
    block_listing = api_client.get_blocks()["data"]
    ret = []
    for block in block_listing:
        if block["name"] == data_block_name:
            ret.append(block)
            break

    for block in block_listing:
        if block["name"] == processing_block_name:
            ret.append(block)
            break

    return ret


# Create workflow and initialize the tasks based on target parameters.
def initialize_workflow():
    targets = extract_target_blocks()
    workflow = api_client.create_workflow(randomized_name, 'Temp workflow for covid19 script')
    api_client.set_workflow_tasks(workflow['data']['id'], targets)
    return workflow['data']['id']


# Run a job with randomized name and templated parameters
def run_job(workflow_id, polygon, time_period, is_dry):
    job_params = {
        processing_block_name: {},
        data_block_name: {
            "ids": None,
            "time": time_period,
            "limit": 1,
            "intersects": {
                "type": "Polygon",
                "coordinates": [
                    polygon
                ]
            },
            "zoom_level": 18,
            "time_series": None,
            "max_cloud_cover": 100,
            "panchromatic_band": False
        }
    }
    job_name = randomized_name + "_job_" + (''.join(random.choice("0123456789abcdef") for i in range(16)))
    return api_client.run_job(workflow_id, job_name, is_dry, job_params)


# await job completion for up to ~tries * 5 seconds. Defaults to ~25 minutes.
def await_job_completion(job, tries=300):
    try_counter = 0
    if DEBUG_LOGGING:
        print("[+] Awaiting job completion")
    while try_counter < tries:
        job_status = api_client.get_job(job['data']['id'])
        extracted_status = job_status['data']['status']
        if extracted_status == 'FAILED':
            return False

        if extracted_status == 'SUCCEEDED':
            return True

        try_counter += 1
        time.sleep(5)
    return False


# Process 1 polygon/time period pair
def get_one_polygon(polygon_num, time_num, workflow_id, polygon, time_period):
    if DEBUG_LOGGING:
        print("[+] Running test query first")
    job = run_job(workflow_id, polygon, time_period, True)
    is_success = await_job_completion(job)
    # We can't get this time period. Output a -
    if not is_success:
        if DEBUG_LOGGING:
            print("[-] Job failed")
        return "-"

    if DEBUG_LOGGING:
        test_job_output = api_client.get_job_output(job['data']['id'])
        print("[+] Acquisition date: " + test_job_output["features"][0]["properties"]["acquisitionDate"])
        print("[+] Estimated credits: " + str(test_job_output["features"][0]["estimatedCredits"]))
        print("[+] Now running actual job")

    job = run_job(workflow_id, polygon, time_period, False)
    is_success = await_job_completion(job)
    if not is_success:
        if DEBUG_LOGGING:
            print("[-] Job failed")
        return "-"

    actual_output = api_client.get_job_output(job['data']['id'])
    if SAVE_ALL_JOB_RESULTS:
        if DEBUG_LOGGING:
            print("[+] Storing job results")
        tasks = api_client.get_job_tasks(job['data']['id'])['data']
        task_num = 0
        for task in tasks:
            task_num += 1
            url = api_client.get_task_signed_url(job['data']['id'], task['id'])['data']['url']
            api_client.dump_task_url(url, folder,
                                     "polygon_" + str(polygon_num) + "_timestamp_" + str(time_num) + "_task_" + str(
                                         task_num) + ".tar.gz")
    return str(len(actual_output["features"][0]["properties"]["det_details"]))


if __name__ == '__main__':
    if DEBUG_LOGGING:
        print("[+] Creating workflow...")
    workflow_id = initialize_workflow()
    if DEBUG_LOGGING:
        print("[+] Created workflow: " + workflow_id)

    polygon_num = 0
    for polygon in POLYGONS:
        polygon_num += 1
        time_limit_num = 0
        for time_limit in TIME_LIMITS:
            time_limit_num += 1
            print(
                "Polygon " + str(polygon_num) + " interval " + str(time_limit_num) + ": " + get_one_polygon(polygon_num,
                                                                                                            time_limit_num,
                                                                                                            workflow_id,
                                                                                                            polygon,
                                                                                                            time_limit))

    # This may be useful if the user wants to manually download or view detection data later in UI or API.
    if CLEANUP_WORKFLOW:
        if DEBUG_LOGGING:
            print("[+] Cleaning workflow up")
        api_client.delete_workflow(workflow_id)
