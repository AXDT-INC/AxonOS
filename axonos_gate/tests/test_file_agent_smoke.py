import os
import subprocess
import time
import http.client
import json
import shutil

PORT = 8799
KEY = "smoke_test_secret_key"
ROOT_DIR = "/tmp/smoke_files_root"
KEY_FILE = "/tmp/smoke_files_key"

def run_smoke_test():
    # 1. Setup clean directory and key file
    if os.path.exists(ROOT_DIR):
        shutil.rmtree(ROOT_DIR)
    os.makedirs(ROOT_DIR)
    
    with open(KEY_FILE, "w") as f:
        f.write(KEY)
        
    print("Starting file_agent.py process...")
    # 2. Start the file agent process in the background
    env = os.environ.copy()
    env["AXGT_FILES_PORT"] = str(PORT)
    env["AXGT_SESSION_FILES_KEY"] = KEY
    env["AXGT_FILES_ROOT"] = ROOT_DIR
    env["AXGT_FILES_KEY_FILE"] = KEY_FILE
    env["AXGT_FILES_BIND_HOST"] = "127.0.0.1"
    
    agent_proc = subprocess.Popen(
        ["python3", "axonos_gate/file_agent.py"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    time.sleep(1.5)  # Wait for agent to start listening
    
    if agent_proc.poll() is not None:
        stdout, stderr = agent_proc.communicate()
        print(f"Agent failed to start. stdout: {stdout}, stderr: {stderr}")
        return False

    try:
        conn = http.client.HTTPConnection("127.0.0.1", PORT)
        
        # Helper for requests
        def request(method, path, headers=None, body=None):
            headers = headers or {}
            conn.request(method, path, body, headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp.headers, data

        print("\n--- Test 1: GET /healthz (Unauthenticated) ---")
        status, _, data = request("GET", "/healthz")
        print(f"Status: {status}, Data: {data.decode()}")
        assert status == 200, "Healthz failed"
        
        print("\n--- Test 2: GET /list (Unauthenticated -> Forbidden) ---")
        status, _, data = request("GET", "/list?path=")
        print(f"Status: {status}, Data: {data.decode()}")
        assert status == 403, "Unauthenticated /list did not return 403"
        
        headers = {"X-AxonOS-Files-Key": KEY}
        
        print("\n--- Test 3: GET /list (Authenticated -> Success Empty) ---")
        status, _, data = request("GET", "/list?path=", headers)
        print(f"Status: {status}, Data: {data.decode()}")
        assert status == 200, "/list failed"
        res = json.loads(data.decode())
        assert len(res["entries"]) == 0, "List should be empty initially"
        
        print("\n--- Test 4: PUT /upload Chunk 1 (offset 0, total 10) ---")
        # Upload first 5 bytes "ABCDE"
        headers_put = {
            "X-AxonOS-Files-Key": KEY,
            "Content-Type": "application/octet-stream",
            "Content-Length": "5"
        }
        status, _, data = request("PUT", "/upload?path=test.txt&offset=0&total=10", headers_put, b"ABCDE")
        print(f"Status: {status}, Data: {data.decode()}")
        assert status == 200, "First chunk upload failed"
        res = json.loads(data.decode())
        assert res["offset"] == 5, "Offset should be 5"
        assert not res["complete"], "Upload should not be complete"
        
        print("\n--- Test 5: GET /upload-status (Resume check) ---")
        status, _, data = request("GET", "/upload-status?path=test.txt&total=10&offset=0", headers)
        print(f"Status: {status}, Data: {data.decode()}")
        assert status == 200, "Upload status check failed"
        res = json.loads(data.decode())
        assert res["offset"] == 5, "Offset should be 5"
        assert not res["exists"], "Final file should not exist yet"
        
        print("\n--- Test 6: PUT /upload Chunk 2 (offset 5, total 10 -> Completed) ---")
        # Upload last 5 bytes "FGHIJ"
        headers_put["Content-Length"] = "5"
        status, _, data = request("PUT", "/upload?path=test.txt&offset=5&total=10", headers_put, b"FGHIJ")
        print(f"Status: {status}, Data: {data.decode()}")
        assert status == 200, "Second chunk upload failed"
        res = json.loads(data.decode())
        assert res["offset"] == 10, "Offset should be 10"
        assert res["complete"], "Upload should be complete"
        
        print("\n--- Test 7: GET /list (Verify file exists) ---")
        status, _, data = request("GET", "/list?path=", headers)
        print(f"Status: {status}, Data: {data.decode()}")
        res = json.loads(data.decode())
        assert len(res["entries"]) == 1, "List should contain exactly 1 entry"
        assert res["entries"][0]["name"] == "test.txt"
        assert res["entries"][0]["size"] == 10, "Size should be 10"
        
        print("\n--- Test 8: GET /download (Full Download) ---")
        status, _, data = request("GET", "/download?path=test.txt", headers)
        print(f"Status: {status}, Content: {data.decode()}")
        assert status == 200, "Full download failed"
        assert data == b"ABCDEFGHIJ", "Content mismatch"
        
        print("\n--- Test 9: GET /download (Range Query: bytes=2-6 -> CDEFG) ---")
        headers_range = {
            "X-AxonOS-Files-Key": KEY,
            "Range": "bytes=2-6"
        }
        status, resp_headers, data = request("GET", "/download?path=test.txt", headers_range)
        print(f"Status: {status}, Content: {data.decode()}")
        assert status == 206, "Range query should return status 206 Partial Content"
        assert data == b"CDEFG", f"Expected CDEFG, got {data.decode()}"
        
        print("\n--- Test 10: GET /download (Range Query: bytes=5- -> FGHIJ) ---")
        headers_range["Range"] = "bytes=5-"
        status, _, data = request("GET", "/download?path=test.txt", headers_range)
        print(f"Status: {status}, Content: {data.decode()}")
        assert status == 206
        assert data == b"FGHIJ"
        
        print("\n--- Smoke Test Passed! ---")
        return True
    finally:
        # Terminate background agent
        print("Terminating agent process...")
        agent_proc.terminate()
        try:
            agent_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            agent_proc.kill()
        
        # Cleanup
        if os.path.exists(ROOT_DIR):
            shutil.rmtree(ROOT_DIR)
        if os.path.exists(KEY_FILE):
            os.remove(KEY_FILE)

if __name__ == "__main__":
    success = run_smoke_test()
    if not success:
        os._exit(1)
