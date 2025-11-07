import grpc
import sys
import os
import time  # Added for retries

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service_pb2
import service_pb2_grpc


class CollaborationClient:
    """
    A smart client that can handle Raft leader redirection and node failure.
    """

    def __init__(self, server_addresses):
        """
        Initializes the client with a list of all server node addresses.
        Args:
            server_addresses (list): e.g., ['localhost:50053', 'localhost:50054']
        """
        if not server_addresses:
            raise ValueError("server_addresses list cannot be empty")

        self.server_addresses = server_addresses
        self.current_leader_address = server_addresses[0]  # Start with the first node
        self.token = None
        self.username = None
        self.channel = None
        self.stub = None

        # Connect to the initial node
        self.connect(self.current_leader_address)

    def connect(self, address):
        """
        Closes the current channel (if any) and connects to a new node.
        """
        if self.channel:
            self.channel.close()

        print(f"\n[Client connecting to node: {address}]")
        self.channel = grpc.insecure_channel(address)
        self.stub = service_pb2_grpc.CollaborationServiceStub(self.channel)
        self.current_leader_address = address

    def _execute_rpc(self, rpc_method_name, request):
        """
        A wrapper to execute any RPC call with full retry and redirect logic.
        This version is robust and handles token expiration on leader change.
        """
        max_retries = len(self.server_addresses) * 2 + 2

        try:
            current_node_index = self.server_addresses.index(self.current_leader_address)
        except ValueError:
            current_node_index = 0
            self.connect(self.server_addresses[0])

        for attempt in range(max_retries):
            try:
                rpc_call = getattr(self.stub, rpc_method_name)
                response = rpc_call(request)

                # --- Handle Successful Response ---
                if response.status == "SUCCESS":
                    return response

                # --- Handle "Not Leader" Failure (Safely) ---
                leader_redirect = False
                redirect_address = None

                if hasattr(response, 'message') and response.message.startswith("Not leader"):
                    print(f"✗ {response.message}")
                    leader_redirect = True
                    redirect_address = response.message.split("Not leader. Try connecting to: ")[1]

                if leader_redirect:
                    if redirect_address == "None":
                        print("...No leader elected yet. Retrying in 2 seconds...")
                        time.sleep(2)
                    else:
                        if redirect_address in self.server_addresses:
                            print(f"...Redirecting to new leader at {redirect_address}...")
                            self.connect(redirect_address)
                            current_node_index = self.server_addresses.index(redirect_address)
                        else:
                            print(f"✗ Error: Leader {redirect_address} not in known server list.")
                            return None
                    continue  # Go to the next loop iteration (retry)

                # --- NEW FIX: Handle Invalid Token on Leader Change ---
                if hasattr(response, 'message') and response.message == "Invalid token":
                    print("✗ Session token is invalid (leader may have changed).")
                    print("Please log in again to establish a new session.")
                    # Force a logout on the client side
                    self.token = None
                    self.username = None
                    return None  # Abort the command
                # --- END FIX ---

                # --- Handle other application failures ---
                if hasattr(response, 'message'):
                    print(f"✗ Server Error: {response.message}")
                else:
                    print(f"✗ Server returned status: {response.status} (no message)")
                return None

            except grpc.RpcError as e:
                # --- Handle Node Down / Connection Failure ---
                if e.code() == grpc.StatusCode.UNAVAILABLE:
                    print(f"✗ Node {self.current_leader_address} is unavailable.")

                    current_node_index = (current_node_index + 1) % len(self.server_addresses)
                    next_node_address = self.server_addresses[current_node_index]

                    print(f"...Trying next node: {next_node_address}...")
                    self.connect(next_node_address)
                else:
                    print(f"✗ gRPC Error: {e.details()}")
                    time.sleep(1)

            except Exception as e:
                print(f"✗ Client-side error (potential bug): {e}")
                return None

        print("✗ Command failed after all retries. Is the cluster down?")
        return None

    def login(self, username, password):
        request = service_pb2.LoginRequest(username=username, password=password)

        # Login is special, it doesn't need the full retry logic
        # as it doesn't go through Raft. But we still need to find *a* server.
        try:
            response = self.stub.Login(request)
        except grpc.RpcError:
            print("Login failed: server unavailable. Trying to find another node...")
            # We must wrap this in the retry logic in case the node we hit is down
            response = self._execute_rpc("Login", request)
            if not response:
                print("✗ Login failed. No servers available.")
                return False

        if response.status == "SUCCESS":
            self.token = response.token
            self.username = username
            print(f"✓ Login successful! Welcome, {username}")
            return True
        else:
            print("✗ Login failed. Check your credentials.")
            return False

    def logout(self):
        if not self.token:
            print("Not logged in")
            return

        request = service_pb2.LogoutRequest(token=self.token)
        response = self._execute_rpc("Logout", request)

        if response:
            print(f"Logout: {response.message}")

        self.token = None
        self.username = None

    def create_document(self, content):
        if not self.token:
            print("Please login first")
            return

        request = service_pb2.PostRequest(
            token=self.token,
            type="document",
            data=content
        )
        response = self._execute_rpc("Post", request)

        if response:  # _execute_rpc returns None on failure
            print(f"✓ {response.message}")

    def update_document(self, doc_id, content):
        if not self.token:
            print("Please login first")
            return

        request = service_pb2.PostRequest(
            token=self.token,
            type="update",
            data=f"{doc_id}|{content}"
        )
        response = self._execute_rpc("Post", request)

        if response:
            print(f"✓ {response.message}")

    def get_documents(self):
        if not self.token:
            print("Please login first")
            return

        request = service_pb2.GetRequest(
            token=self.token,
            type="documents",
            params=""
        )
        response = self._execute_rpc("Get", request)

        if response and response.status == "SUCCESS":
            print("\n=== Documents ===")
            if not response.items:
                print("(No documents found)")
            for item in response.items:
                print(f"ID: {item.id}")
                print(f"Data: {item.data}")
                print("-" * 50)
        elif not response:
            pass  # Error was already printed by _execute_rpc
        else:
            print("Failed to retrieve documents")

    def get_active_users(self):
        if not self.token:
            print("Please login first")
            return

        request = service_pb2.GetRequest(
            token=self.token,
            type="active_users",
            params=""
        )
        response = self._execute_rpc("Get", request)

        if response and response.status == "SUCCESS":
            print("\n=== Active Users ===")
            if not response.items:
                print("(No active users)")
            for item in response.items:
                print(f"• {item.data}")
        elif not response:
            pass
        else:
            print("Failed to retrieve active users")

    def query_llm(self, query):
        if not self.token:
            print("Please login first")
            return

        request = service_pb2.GetRequest(
            token=self.token,
            type="llm_query",
            params=query
        )
        response = self._execute_rpc("Get", request)

        if response and response.status == "SUCCESS" and response.items:
            print("\n=== LLM Response ===")
            print(response.items[0].data)
        elif not response:
            pass
        else:
            print("LLM query failed")

    def interactive_menu(self):
        while True:
            print("\n" + "=" * 50)
            print("Distributed Collaboration Platform - Milestone 1")
            print("=" * 50)

            if not self.token:
                print("\n1. Login")
                print("2. Exit")
                choice = input("\nChoice: ")

                if choice == "1":
                    username = input("Username: ")
                    password = input("Password: ")
                    self.login(username, password)
                elif choice == "2":
                    break
            else:
                print(f"\nLogged in as: {self.username} (Connected to {self.current_leader_address})")
                print("\n1. Create Document")
                print("2. View All Documents")
                print("3. View Active Users")
                print("4. Query LLM (Grammar/Summarize/Suggest)")
                print("5. Logout")
                print("6. Exit")

                choice = input("\nChoice: ")

                if choice == "1":
                    content = input("Document content: ")
                    self.create_document(content)
                elif choice == "2":
                    self.get_documents()
                elif choice == "3":
                    self.get_active_users()
                elif choice == "4":
                    query = input("Enter your query (e.g., 'check grammar', 'summarize', 'suggest improvements'): ")
                    self.query_llm(query)
                elif choice == "5":
                    self.logout()
                elif choice == "6":
                    if self.token:
                        self.logout()
                    break


def main():
    # --- IMPORTANT ---
    # Define all your App Server nodes here
    SERVER_NODES = [
        "localhost:50053",
        "localhost:50054",
        "localhost:50055"
    ]

    print("Connecting to Application Server cluster...")
    try:
        client = CollaborationClient(SERVER_NODES)
    except grpc.RpcError as e:
        print(f"✗ Failed to connect to initial node: {e.details()}")
        print("Please ensure at least one server node is running.")
        return
    except Exception as e:
        print(f"✗ Failed to initialize client: {e}")
        return

    print("\n" + "=" * 50)
    print("Available test credentials:")
    print("  Username: admin  | Password: admin123")
    print("  Username: user1  | Password: password1")
    print("  Username: user2  | Password: password2")
    print("=" * 50)

    client.interactive_menu()
    print("\nGoodbye!")


if __name__ == '__main__':
    main()