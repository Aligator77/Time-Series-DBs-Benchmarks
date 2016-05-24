#!/usr/bin/python

# Using argparse to parse cli arguments
import argparse

# Import threading essentials
from itertools import cycle, islice
from threading import Lock, Thread, Condition

# For randomizing
import string

import random
from random import choice

# To get the time ,datetime
import time
from datetime import datetime

# For misc
import sys

# For json operations
import json
import traceback

import benchmark_helper
from benchmark_helper import increment_success, increment_failure, increment_size, has_timeout
from benchmark_helper import print_stats, print_stats_worker
from metrics_generator import fill_metrics_pool

# Try and import Elasticsearch
try:
    from elasticsearch import Elasticsearch, helpers, serializer
    from elasticsearch.client import IndicesClient

except:
    print("Could not import elasticsearch..")
    print("Try: pip install elasticsearch")
    sys.exit(1)

# Set a parser object
parser = argparse.ArgumentParser()

### Mandatory Parameters
parser.add_argument("min_num_of_clients",
                    type=int, default=1,
                    help="The minimum number of client threads")

parser.add_argument("max_num_of_clients",
                    type=int, default=2,
                    help="The maximum number of client threads")

# parser.add_argument("number_of_clients",
#                     type=int,
#                     help="The number of threads to write from")

parser.add_argument("running_seconds",
                    type=int,
                    help="The number of seconds to run")

### Optional Parameters
parser.add_argument("--es-hosts",
                    nargs='+', default=["10.10.10.10", "10.10.10.11"],
                    help="The address of your cluster (no protocol or port), (default ['10.10.10.10','10.10.10.11'])")

parser.add_argument("--indices",
                    type=int, default=8,
                    help="The number of indices to write to (default 8)")

parser.add_argument("--number-of-shards",
                    type=int, default=5,
                    help="Number of shards per index (default 5)")

parser.add_argument("--number-of-replicas",
                    type=int, default=1,
                    help="Number of replicas per index (default 1)")

parser.add_argument("--refresh_interval",
                    type=int, default=1,
                    help="Index refresh interval (default 1)")

parser.add_argument("--number-of-metrics-per-bulk",
                    type=int, default=60000,
                    help="Number of metric docs per bulk request (default 60000),If too high, HTTP content length would exceeded limited bytes.")

parser.add_argument("--no-cleanup",
                    dest='cleanup', action='store_false',
                    help="Don't delete the indices upon finish (Default:cleanup=True, --no-cleanup turn it to False)")

parser.set_defaults(cleanup=True)

parser.add_argument("--stats-interval",
                    type=int, default=30,
                    help="Number of seconds to wait between stats prints (default 30)")

# Parse the arguments
args = parser.parse_args()

# Set variables from argparse output (for readability)
MIN_NUM_OF_CLIENTS = args.min_num_of_clients
MAX_NUM_OF_CLIENTS = args.max_num_of_clients
# NUMBER_OF_CLIENTS =          args.number_of_clients
RUNNING_SECONDS   =          args.running_seconds

ES_HOSTS    =                args.es_hosts
NUMBER_OF_INDICES =          args.indices
NUMBER_OF_SHARDS =           args.number_of_shards
NUMBER_OF_REPLICAS =         args.number_of_replicas
REFRESH_INTERVAL   =         args.refresh_interval
NUMBER_OF_METRICS_PER_BULK = args.number_of_metrics_per_bulk
CLEANUP =                    args.cleanup
INTERVAL_BETWEEN_STATS =     args.stats_interval

# Constant
SYS_MAXINT = sys.maxint

metrics_pool_dict = {
        'long_metrics':    [],
        'integer_metrics': [],
        'short_metrics':   [],
        'byte_metrics':    [],
        'double_metrics':  [],
        'float_metrics':   [],
        'boolean_metrics': []
        }


indices = []

types = ["long_metrics",
         "integer_metrics",
         "short_metrics",
         "byte_metrics",
         "double_metrics",
         "float_metrics",
         "boolean_metrics"]

es = None # Will hold the elasticsearch session
es_indices = None # elasticsearch.client import IndicesClient

settings_body = {"settings":
                     {
                         "number_of_shards":   NUMBER_OF_SHARDS,
                         "number_of_replicas": NUMBER_OF_REPLICAS,
                         "index" : {
                            "refresh_interval": str(REFRESH_INTERVAL)+"s"
                         }
                     }
                 }

mappings_body = {
        "_default_": {
              "dynamic_templates": [
                    {
                      "strings": {
                        "match": "*",
                        "match_mapping_type": "string",
                        "mapping":   { "type": "string",  "doc_values": True, "index": "not_analyzed" }
                      }
                    }
                ],
                "_all":            { "enabled": True},
                "_source":         { "enabled": True},
                "properties": {
                    "@timestamp":  { "type": "date",    "doc_values": True}
                }
        },
        "long_metrics": {
              "properties": {
                "long_value":    { "type": "long",    "doc_values": True}
              }
        },
        "integer_metrics": {
              "properties": {
                "integer_value": { "type": "integer", "doc_values": True}
              }
        },
        "short_metrics": {
              "properties": {
                "short_value":   { "type": "short",   "doc_values": True}
              }
        },
        "byte_metrics": {
              "properties": {
                "byte_value":    { "type": "byte",    "doc_values": True}
              }
        },
        "double_metrics": {
              "properties": {
                "double_value":  { "type": "double",  "doc_values": True}
              }
        },
        "float_metrics": {
              "properties": {
                "float_value":   { "type": "float",   "doc_values": True}
              }
        },
        "boolean_metrics": {
              "properties": {
                "boolean_value": { "type": "boolean", "doc_values": True}
              }
        }
}

# Register specific mapping definition for a specific type.
def put_mapping():
    es_indices.put_mapping(doc_type="_default_", body=mappings_body["_default_"], index="_all" )
    for type_name in types:
        es_indices.put_mapping(doc_type=type_name, body=mappings_body[type_name], index="_all" )

def print_mapping():
    # Retrieve mapping definition of index or index/type.
    print json.dumps(es_indices.get_mapping(index=["metrics_0", "metrics_1"],doc_type=types),
                     sort_keys=True, indent=4, separators=(',', ': '))

def bulk_metrics_worker():
    # Running until timeout
    while not has_timeout():

        cur_bulk  =""

        # Iterate over the bulk size
        for _ in range(NUMBER_OF_METRICS_PER_BULK):
            #Randomly pick a type
            type_name = choice(types)
            # Generate the bulk operation, here {0} means the 0-th argument
            cur_bulk += "{0}\n".format(json.dumps({"index": {"_index": choice(indices), "_type": type_name}}))
            cur_bulk += "{0}\n".format(json.dumps(  choice(metrics_pool_dict[type_name]))  )

        try:
            # Perform the bulk operation !!!!!!!!!!!!!!!!!!
            es.bulk(body=cur_bulk)
            # Adding to success bulks
            increment_success()
            # Adding to size (in bytes)
            increment_size(sys.getsizeof(str(cur_bulk)))

        except Exception as e:
            # Failed. incrementing failure
            increment_failure()
            print(e.message)
            traceback.print_exc()

def generate_clients(num_of_clients):
    # Clients placeholder
    list_clients = []
    # Iterate over the clients count
    for _ in range(num_of_clients):
        a_client_thread = Thread(target=bulk_metrics_worker)
        a_client_thread.daemon = True
        # Create a thread and push it to the list
        list_clients.append(a_client_thread)

    # Return the clients
    return list_clients


def generate_indices():
    # Placeholder
    list_indices = []
    # Iterate over the indices count
    for i in range(NUMBER_OF_INDICES):
        # Generate the index name
        index_name = "metrics_"+str(i)
        # Push it to the list
        list_indices.append(index_name)

        try:
            # And create it in ES with the shard count and replicas
            es.indices.create(index=index_name, body=settings_body)

        except:
            print("Could not create index. Is your cluster ok?")
            sys.exit(1)

    # Return the indices
    return list_indices


def cleanup_indices():
    # Iterate over all indices and delete those
    for cur_index in indices:
        try:
            # Delete the index
            es.indices.delete(index=cur_index, ignore=[400, 404])

        except:
            print("Could not delete index: {0}. Continue anyway..".format(cur_index))


def check_paras():
    global MIN_NUM_OF_CLIENTS
    global MAX_NUM_OF_CLIENTS
    global RUNNING_SECONDS
    if MIN_NUM_OF_CLIENTS < 0:
        MIN_NUM_OF_CLIENTS = 1
    if MAX_NUM_OF_CLIENTS < 0 or MAX_NUM_OF_CLIENTS < 1:
        MAX_NUM_OF_CLIENTS = MIN_NUM_OF_CLIENTS + 1
    if RUNNING_SECONDS < 0:
        RUNNING_SECONDS = 60


def test_case_of_n_clients(num_of_clients):
    # Define the globals
    global indices
    global es
    global es_indices
    global metrics_pool_dict
    try:
        # Initiate the elasticsearch session using ES low-level client.
        # By default nodes are randomized before passed into the pool and round-robin strategy is used for load balancing.
        es = Elasticsearch(ES_HOSTS, timeout=30)
        es_indices = IndicesClient(es)

    except:
        print("Could not connect to elasticsearch!")
        sys.exit(1)

    print("Cleaning up created indices.. "),
    cleanup_indices()
    print("Done!\n")

    metrics_pool_dict = fill_metrics_pool(**metrics_pool_dict)

    print("Generating  clients.. "),
    clients = generate_clients(num_of_clients)
    print("Done!\n")

    print("Creating indices.. "),
    indices = generate_indices()
    print("Done!\n")

    put_mapping()
    # print_mapping()

    STARTED_TIMESTAMP = int(time.time())
    benchmark_helper.init(STARTED_TIMESTAMP, RUNNING_SECONDS, INTERVAL_BETWEEN_STATS, NUMBER_OF_METRICS_PER_BULK)

    print("Starting the test. Will print stats every {0} seconds.".format(INTERVAL_BETWEEN_STATS))
    print("The test would run for {0} seconds, but it might take a bit more "
          "because we are waiting for current bulk operation to complete. \n".format(RUNNING_SECONDS))

    # Run the clients!
    map(lambda thread: thread.start(), clients)

    # Create and start the print stats thread
    stats_thread = Thread(target=print_stats_worker(num_of_clients))
    stats_thread.daemon = True
    stats_thread.start()

    # And join them all but the stats, that we don't care about
    map(lambda thread: thread.join(), clients)
    # A call to thread1.join() blocks the thread in which you're making the call, until thread1 is finished.
    # It's like wait_until_finished(thread1).

    # Record final results into report.txt file
    is_final_result = True
    print_stats(num_of_clients, is_final_result)

    # We will Clean up the indices by default
    # Default: True
    if CLEANUP:
        print("Cleaning up created indices.. "),
        cleanup_indices()
        print("Done!\n\n\n\n")


def main():
    check_paras()
    for number_of_clients in range(MIN_NUM_OF_CLIENTS, MAX_NUM_OF_CLIENTS+1):  # 1 client to 100 clients
        test_case_of_n_clients(number_of_clients)
        time.sleep(RUNNING_SECONDS+15)
    sys.exit(-1)


if __name__ == "__main__":
    # Main runner
    try:
        main()

    except Exception as e:
        print("Got unexpected exception. probably a bug, please report it.")
        print("")
        print(e.message)
        traceback.print_exc()
        sys.exit(1)
