import grpc
import sys
import os
import time
import threading
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service_pb2
import service_pb2_grpc

class CollaborationClient:
    def __init__(self, server_addresses):
        if not server_addresses:
            raise ValueError("server_addresses list cannot be empty")

        self.server_addresses=server_addresses
        self.current_leader_address=server_addresses[0]
        self.token=None
        self.username=None
        self.channel=None
        self.stub=None

        self.stop_listening=threading.Event()
        self.listener_thread=None
        self.connect(self.current_leader_address)

    def connect(self, address):
        if self.channel:
            self.channel.close()
        print(f"\n[Client connecting to node: {address}]")
        self.channel=grpc.insecure_channel(address)
        self.stub=service_pb2_grpc.CollaborationServiceStub(self.channel)
        self.current_leader_address=address

    def _listen_loop(self):
        """Background thread to receive real-time updates."""
        while not self.stop_listening.is_set():
            if not self.token or not self.stub:
                time.sleep(1)
                continue

            try:
                request=service_pb2.SubscribeRequest(token=self.token)
                stream=self.stub.Subscribe(request)
                for event in stream:
                    if self.stop_listening.is_set():
                        break

                    if event.type=="CREATE":
                        print(f"\n[ALERT] User '{event.user}' created document {event.doc_id[:8]}...")
                    elif event.type=="UPDATE":
                        print(f"\n[ALERT] User '{event.user}' updated document {event.doc_id[:8]}...")
                    elif event.type=="LOCK":
                        print(f"\n[ALERT] Document {event.doc_id[:8]}... was LOCKED by '{event.user}'")
                    elif event.type=="UNLOCK":
                        print(f"\n[ALERT] Document {event.doc_id[:8]}... was UNLOCKED by '{event.user}'")
                    # --- NEW: Presence Alerts ---
                    elif event.type=="JOIN":
                         print(f"\n[ALERT] User '{event.user}' has joined.")
                    elif event.type=="LEAVE":
                         print(f"\n[ALERT] User '{event.user}' has left.")

            except grpc.RpcError:
                time.sleep(2)
            except Exception:
                time.sleep(2)

    def _execute_rpc(self, rpc_method_name, request):
        max_retries=len(self.server_addresses)*2+2
        try:
            current_node_index=self.server_addresses.index(self.current_leader_address)
        except ValueError:
            current_node_index=0
            if not self.server_addresses:
                print("Error: No server addresses configured.")
                return None
            self.connect(self.server_addresses[0])

        for attempt in range(max_retries):
            try:
                rpc_call=getattr(self.stub, rpc_method_name)
                response=rpc_call(request)

                if response.status=="SUCCESS":
                    return response

                leader_message=None

                if response.status.startswith("NOT_LEADER|"):
                    leader_message=response.status
                elif hasattr(response, 'message') and response.message.startswith("NOT_LEADER|"):
                    leader_message=response.message

                if leader_message:
                    print(f"Node {self.current_leader_address} is a follower. Receiving cluster info...")
                    parts=leader_message.split('|')
                    new_leader=None
                    new_peers=[]

                    for part in parts[1:]:
                        if part.startswith("leader_id="):
                            new_leader=part.split('=', 1)[1]
                        if part.startswith("peers="):
                            new_peers=part.split('=', 1)[1].split(',')

                    if new_peers:
                        print(f"...Updating known nodes to: {new_peers}")
                        self.server_addresses=list(dict.fromkeys(new_peers))

                    if new_leader and new_leader!="None" and new_leader in self.server_addresses:
                        print(f"...Redirecting to new leader at {new_leader}...")
                        self.connect(new_leader)
                        current_node_index=self.server_addresses.index(new_leader)
                        continue
                    else:
                        print("...No valid leader hint. Trying next known node...")

                elif hasattr(response,'message') and response.message=="Invalid token":
                    print("Session token is invalid (leader may have changed).")
                    print("Please log in again to establish a new session.")
                    self.token=None
                    self.username=None
                    return None

                elif hasattr(response, 'message'):
                    print(f"Server Error: {response.message}")
                else:
                    print(f"Server returned status: {response.status}")
                return None

            except grpc.RpcError as e:
                if e.code()==grpc.StatusCode.UNAVAILABLE:
                    print(f"Node {self.current_leader_address} is unavailable.")
                else:
                    print(f"gRPC Error: {e.details()}")

            except Exception as e:
                print(f"Client-side error: {e}")
                return None

            current_node_index=(current_node_index + 1)%len(self.server_addresses)
            next_node_address=self.server_addresses[current_node_index]
            print(f"...Trying next node: {next_node_address}...")
            self.connect(next_node_address)
            time.sleep(1)

        print("Command failed after all retries. Is the cluster down?")
        return None

    def login(self, username, password):
        if self.listener_thread and self.listener_thread.is_alive():
            self.stop_listening.set()
            self.listener_thread.join()

        request=service_pb2.LoginRequest(username=username, password=password)
        response=self._execute_rpc("Login", request)

        if response and response.status == "SUCCESS":
            self.token=response.token
            self.username=username
            print(f"Login successful! Welcome, {username}")

            self.stop_listening.clear()
            self.listener_thread=threading.Thread(target=self._listen_loop, daemon=True)
            self.listener_thread.start()
            return True
        else:
            print("Login failed.")
            return False

    def logout(self):
        if not self.token: return
        request=service_pb2.LogoutRequest(token=self.token)
        response=self._execute_rpc("Logout", request)
        if response: print(f"Logout: {response.message}")

        self.stop_listening.set()
        self.token=None
        self.username=None

    def create_document(self, content):
        if not self.token: return
        doc_id=str(uuid.uuid4())
        data = f"{doc_id}|{content}"
        request=service_pb2.PostRequest(token=self.token, type="document", data=data)
        res = self._execute_rpc("Post", request)
        if res:
            print(f"{res.message} (DocID: {doc_id[:8]}...)")

    def update_document(self, doc_id, content):
        if not self.token: return
        request=service_pb2.PostRequest(token=self.token, type="update", data=f"{doc_id}|{content}")
        res=self._execute_rpc("Post", request)
        if res: print(f"{res.message}")

    def lock_document(self, doc_id):
        if not self.token: return
        request=service_pb2.PostRequest(token=self.token, type="lock", data=doc_id)
        res=self._execute_rpc("Post", request)
        if res: print(f"{res.message}")

    def unlock_document(self, doc_id):
        if not self.token: return
        request=service_pb2.PostRequest(token=self.token, type="unlock", data=doc_id)
        res = self._execute_rpc("Post", request)
        if res: print(f"{res.message}")

    def get_documents(self):
        if not self.token: return
        res=self._execute_rpc("Get", service_pb2.GetRequest(token=self.token, type="documents", params=""))
        if res and res.status=="SUCCESS":
            print("\n===== Documents =====")
            for item in res.items:
                print(f"ID: {item.id}")
                print(f"Data: {item.data}")
                print("-"*50)

    def get_active_users(self):
        if not self.token: return
        res=self._execute_rpc("Get", service_pb2.GetRequest(token=self.token, type="active_users", params=""))
        if res and res.status=="SUCCESS":
            print("\n===== Active Users =====")
            for item in res.items: print(f"• {item.data}")

    def query_llm(self, query, context=""):
        if not self.token: return
        req=service_pb2.GetRequest(token=self.token, type="llm_query", params=query, context=context)
        res=self._execute_rpc("Get", req)
        if res and res.status=="SUCCESS" and res.items:
            print(f"\n===== LLM Response =====\n{res.items[0].data}")

    def summarize_document(self, doc_id):
        print(f"Fetching content for document {doc_id}...")
        req = service_pb2.GetRequest(token=self.token, type="document_content", params=doc_id)
        res = self._execute_rpc("Get", req)
        if res and res.status=="SUCCESS" and res.items:
            content=res.items[0].data
            print("Content fetched. Asking LLM to summarize...")
            self.query_llm(query="Summarize this document in 3 concise bullet points.", context=content)
        else:
            print("Failed to fetch document content.")

    def fix_grammar(self, doc_id):
        print(f"Fetching content for document {doc_id}...")
        req=service_pb2.GetRequest(token=self.token, type="document_content", params=doc_id)
        res=self._execute_rpc("Get", req)
        if res and res.status=="SUCCESS" and res.items:
            content=res.items[0].data
            print("Content fetched. Asking LLM to fix grammar...")
            self.query_llm(query="Fix grammar/spelling. Output ONLY the corrected text.", context=content)
        else:
            print("✗ Failed to fetch document content.")

    def view_document_history(self, doc_id):
        if not self.token: return
        req=service_pb2.GetRequest(token=self.token, type="document_history", params=doc_id)
        res=self._execute_rpc("Get", req)

        if res and res.status=="SUCCESS":
            print(f"\n===== History for Doc {doc_id[:8]}... =====")
            if not res.items:
                print("No history found.")
                return

            for item in res.items:
                print(f"- {item.data}")
        else:
            print("Failed to retrieve document history.")

    def interactive_menu(self):
        while True:
            print("\n"+"="*50+"\nDistributed Collaboration Platform\n"+"="*50)
            if not self.token:
                print("\n1. Login\n2. Exit")
                choice=input("\nChoice: ")
                if choice=="1":
                    self.login(input("Username: "), input("Password: "))
                elif choice=="2":
                    break
            else:
                print(f"\nLogged in as: {self.username} (Connected to {self.current_leader_address})")
                print("1. Create Document")
                print("2. Update Document")  # NEW
                print("3. View All Documents")
                print("4. View Active Users")
                print("5. Query LLM (Generic - not context aware queries)")
                print("6. Summarize Document (Context aware queries)")
                print("7. Fix Grammar (Context aware queries)")
                print("8. Lock/Unlock Document")
                print("9. View Document History")
                print("10. Logout")
                print("11. Exit")
                print("12. Add node")

                choice = input("\nChoice: ")
                if choice=="1":
                    self.create_document(input("Document content:"))
                elif choice=="2":
                    self.update_document(input("Document ID:"), input("New content:"))
                elif choice=="3":
                    self.get_documents()
                elif choice=="4":
                    self.get_active_users()
                elif choice=="5":
                    self.query_llm(input("Enter your query:"))
                elif choice=="6":
                    self.summarize_document(input("Enter Doc ID:"))
                elif choice=="7":
                    self.fix_grammar(input("Enter Doc ID:"))
                elif choice=="8":
                    action=input("Enter 'l' to lock or 'u' to unlock:").lower()
                    doc_id=input("Enter Document ID:")
                    if action=='l':
                        self.lock_document(doc_id)
                    elif action=='u':
                        self.unlock_document(doc_id)
                elif choice=="9":
                    self.view_document_history(input("Enter Document ID:"))
                elif choice=="10":
                    self.logout()
                elif choice=="11":
                    if self.token: self.logout()
                elif choice=="12":
                    new_id=input("Enter new node ID (e.g. localhost:50056): ")
                    if not self.token: return
                    request=service_pb2.PostRequest(token=self.token, type="add_node", data=new_id)
                    res=self._execute_rpc("Post", request)
                    if res: print(f"{res.message}")

def main():
    SERVER_NODES=["localhost:50053", "localhost:50054", "localhost:50055"]
    print("Connecting to cluster...")
    try:
        client=CollaborationClient(SERVER_NODES)
        client.interactive_menu()
    except Exception as e:
        print(f"Client failed to start:{e}")


if __name__ == '__main__':
    main()