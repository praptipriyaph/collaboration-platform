# Distributed Real-time Collaboration Platform

This project is a high-availability, fault-tolerant collaboration system built in Python. It implements the Raft consensus protocol from scratch to manage a distributed state machine, uses gRPC for all inter-service communication, and integrates a local LLM for intelligent text assistance.

This system allows multiple users to join a session, edit documents, and see all changes, locks, and presence events propagated in real-time.


## Architecture

The system runs as a distributed cluster of nodes:

LLM Server: A standalone gRPC server that runs a local LLM to handle AI queries.

App Server Nodes(s) (Raft Cluster): A cluster of three servers that form a Raft consensus group. They manage the replicated log, elect a leader, and maintain the document state.

Client Node(s) A "smart" CLI client that connects to the cluster, automatically finds the leader, and subscribes to real-time event streams.

## Features Implemented

#### Raft Consensus: 
Full from-scratch implementation of leader election, log replication, and failure detection.
#### Fault Tolerance: 
The cluster survives the loss of any single node (including the leader) and will elect a new leader.
#### Real-time Updates: 
Uses gRPC server-side streaming (Subscribe) to instantly push document changes (CREATE, UPDATE) to all connected clients.
#### Conflict Resolution: 
A simplified distributed locking mechanism (LOCK, UNLOCK) managed via Raft. Updates are rejected if the user does not hold the lock.
#### Real-time Presence: 
JOIN and LEAVE events are broadcast to all clients as users connect and disconnect from the real-time stream.
#### Local LLM Integration: 
An independent gRPC server (llm_server) loads a GGUF model (e.g., TinyLlama) using ctransformers to provide real grammar, summarization, and content enhancement.
#### Smart Client: 
The CLI client (client.py) automatically retries on failure, handles leader redirection, and listens for real-time alerts in a background thread.


## How to Run the System

### 1. Setup

### Clone the repository
git clone [https://github.com/your-username/collaboration-platform.git](https://github.com/your-username/collaboration-platform.git)
cd collaboration-platform

#### Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

#### Install all Python dependencies
pip install -r requirements.txt


### 2. Download the LLM Model

This project uses TinyLlama-1.1B. You must download the model file (~600MB) into the models/ directory.

#### Create the models directory
mkdir models

#### Download the model file
wget -P models/ [https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf](https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf)


(Note: The LLM server is hardcoded to look for this specific file. If you use a different model, update the path in llm_server/server.py)

### 3. Generate gRPC Code

Compile the .proto file to generate service_pb2.py and service_pb2_grpc.py.

python -m grpc_tools.protoc -Iproto --python_out=. --grpc_python_out=. proto/service.proto


### 4. Launch the 5-Node Cluster

You must run these commands in 5 separate terminals.

Terminal 1: Start the LLM Server (Node 1)

python run_llm_server.py


Wait for it to print: ✓ LLM successfully loaded and ready!

Terminal 2: Start the First Raft Node (Node 2)

python run_app_server.py localhost:50053 localhost:50054 localhost:50055


Terminal 3: Start the Second Raft Node (Node 3)

python run_app_server.py localhost:50054 localhost:50053 localhost:50055


Terminal 4: Start the Third Raft Node (Node 4)

python run_app_server.py localhost:50055 localhost:50053 localhost:50054


Wait ~5 seconds. You will see one of these terminals print: Won election for Term 1...

Terminal 5: Start the Client (Node 5)

python run_client.py


You can now log in and use the system! Try opening a 6th terminal and running the client again to test the real-time alerts between users.
