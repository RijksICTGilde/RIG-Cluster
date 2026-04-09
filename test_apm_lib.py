import os
import time
import argparse
import elasticapm

def run_test_transaction(service_name: str, environment: str):

    print(f"Initializing Elastic APM client for service: {service_name}")
    print(f"Server URL: {os.environ.get('ELASTIC_APM_SERVER_URL', 'http://localhost:8200')}")
    
    client = elasticapm.Client(
        service_name=service_name,
        environment=environment,
        # Other settings can be passed here or set via env vars
    )

    print("Starting transaction: 'test-transaction'...")
    client.begin_transaction("request")
    
    try:
        # Step 1: A simple span
        with elasticapm.capture_span("database-query", "db.postgresql.query"):
            print("Simulating database query...")
            time.sleep(0.1)
        
        # Step 2: A nested span
        execute_subtask()
        
        # Step 3: Simulating an error (optional)
        # try:
        #     1/0
        # except ZeroDivisionError:
        #     client.capture_exception()
        #     print("  Captured simulated exception.")

        client.end_transaction("test-transaction", "success")
        print("Transaction completed and sent to APM Server!")
        
    except Exception as e:
        client.capture_exception()
        client.end_transaction("test-transaction", "failure")
        print(f"✗ Transaction failed: {e}")
    finally:
        # Ensure all data is flushed before exiting
        client.close()

@elasticapm.capture_span("subtask-execution", "task")
def execute_subtask():
    print("Executing subtask...")
    time.sleep(0.05)
    with elasticapm.capture_span("internal-calculation"):
        time.sleep(0.02)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send test APM transaction using 'elastic-apm' library")
    parser.add_argument("--url", help="APM Server URL (ELASTIC_APM_SERVER_URL)")
    parser.add_argument("--token", help="Secret token (ELASTIC_APM_SECRET_TOKEN)")
    parser.add_argument("--service", default="test-service-lib", help="Service name (ELASTIC_APM_SERVICE_NAME)")
    parser.add_argument("--env", default="testing", help="Environment name (ELASTIC_APM_ENVIRONMENT)")
    
    args = parser.parse_args()

    # Override environment variables if arguments are provided
    if args.url:
        os.environ["ELASTIC_APM_SERVER_URL"] = args.url
    if args.token:
        os.environ["ELASTIC_APM_SECRET_TOKEN"] = args.token
    if args.service:
        os.environ["ELASTIC_APM_SERVICE_NAME"] = args.service
    if args.env:
        os.environ["ELASTIC_APM_ENVIRONMENT"] = args.env

    # Verification of configuration
    if not os.environ.get("ELASTIC_APM_SECRET_TOKEN") and not os.environ.get("ELASTIC_APM_API_KEY"):
        print("Warning: No ELASTIC_APM_SECRET_TOKEN or ELASTIC_APM_API_KEY provided.")
        print("If your APM server requires authentication, this script might fail.")

    run_test_transaction(os.environ["ELASTIC_APM_SERVICE_NAME"], os.environ["ELASTIC_APM_ENVIRONMENT"])
