
# **Distributed Collaboration Platform - Final Submission**

**Final Project (Milestone 1) for CS G623 Advanced Operating Systems, Semester I**  
*By Ansh, Praptipriya Phukon, Tathya Sethi*

This project is a high-availability, fault-tolerant collaboration system built in Python. It implements the **Raft consensus protocol** from scratch to manage a distributed state machine, uses **gRPC** for all inter-service communication, and integrates a **local LLM** for intelligent text assistance.

This system allows multiple users to join a session, edit documents, and see all changes, locks, and presence events propagated in real-time. The cluster can survive node failures and even allows for new nodes to be added dynamically without any downtime.

## Features Implemented

#### Core Features
* **gRPC Service Architecture**: All communication between the client, app servers, and LLM server is handled via efficient gRPC protocols defined in `service.proto`.
* **Authentication & Session Management**: Secure user login with password hashing and token-based session management. All session creations are replicated across the Raft cluster.
* **Document Management**: Create, update, and view documents. Includes a simplified locking mechanism (`LOCK`/`UNLOCK`) for conflict resolution, with all state managed by Raft.
* **Real-time Updates**: Uses a gRPC server-side stream (`Subscribe`) to instantly push document changes, locks, and alerts to all connected clients.
* **Presence Awareness**: Real-time "JOIN" and "LEAVE" events are broadcast to all users, showing who is currently online.
* **Version History**: All document edits are saved, and users can retrieve a full version history for any document.

#### Innovative & Advanced Features
* **Raft Consensus from Scratch**: A full implementation of the Raft consensus protocol, including leader election, log replication, and failure detection, to ensure distributed consistency.
* **Fault Tolerance**: The cluster can survive the loss of any single node (including the leader) and will automatically elect a new leader and continue operating.
* **Log Compaction (Snapshotting)**: Implements the `InstallSnapshot` RPC to periodically save the system's state and prune the Raft log, preventing it from growing indefinitely.
* **Local LLM Integration**: A separate gRPC server loads a local GGUF model (`TinyLlama`) to provide true context-aware AI assistance for summarization and grammar correction.
* **Dynamic Cluster Membership**: Ability to add a new server node to the cluster *while it is running* by submitting an `ADD_NODE` command to the Raft log.
* **Smart Client (Dynamic Discovery)**: The client automatically handles leader failures. If it connects to a follower, the follower replies with the current cluster topology, and the client automatically redirects to the new leader.
* **Unit Tests**: Includes a `tests/` directory with `pytest` unit tests to validate the `DocumentManager` logic and `RaftNode` state transitions.

## Architecture

The system is designed to run as a 5-node distributed cluster, with each node as a separate process.


* **Node 1: LLM Server** (`localhost:50052`)
    * A standalone gRPC server that loads the `ctransformers` model and handles AI queries.
* **Nodes 2, 3, 4: App Server (Raft Cluster)** (`localhost:50053-50055`)
    * Three independent application servers that form a Raft consensus group.
    * One server will be elected **Leader** (handles all client writes) and the other two will be **Followers** (replicate the leader's log).
* **Node 5: Client Node**
    * A "smart" CLI client that connects to the cluster, automatically finds the leader, and subscribes to real-time event streams.

## Quick Start (Setup, Deployment, and Usage)

### 1. **Installation**

First, clone the repository. It is highly recommended to use a Python virtual environment.

```bash
# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install all dependencies
pip install -r requirements.txt
```
### 2. **Download LLM Model**

The LLM server requires the TinyLlama GGUF model file.

```bash
# Create the models directory
mkdir models

# Download the model file (approx. 600MB)
wget -P models/ [https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf](https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf)
```

### 3. **Generate gRPC Code**

This step is typically run automatically by the run_ scripts, but you can run it manually to compile the service.proto file.

```bash
python -m grpc_tools.protoc -Iproto --python_out=. --grpc_python_out=. proto/service.proto
```

### 4. **Launch the 5-Node Cluster (Deployment)**

You must run these commands in 5 separate terminals.

```bash
python run_llm_server.py
python run_app_server.py localhost:50053 localhost:50054 localhost:50055
python run_app_server.py localhost:50054 localhost:50053 localhost:50055
python run_app_server.py localhost:50055 localhost:50053 localhost:50054
python run_client.py
```

## Usage Instructions
Once the client is running, you can use the interactive menu:

* **Login**
Login using the username  and password.

* **Interact**
Create a document, view all documents, or view active users.

* **Test Real-time Features**
Open multiple client terminals and see real time alerts.  

* **Test LLM** Context aware queries: Try options 6 (Summarize) or 7 (Fix Grammar) on a document you created.

* **Test Dynamic Membership**:

From your logged-in client, select option 12, "Add node".

Enter localhost:50056. The command will be submitted.

In a new (6th or 7th) terminal, start the new node: 
```bash
python run_app_server.py localhost:50056 localhost:50053 localhost:50054 localhost:50055
```
You will see the leader send it heartbeats and snapshots, adding it to the cluster without downtime.

## Running the Unit Tests
```bash
# From the project root directory
python -m pytest
```
