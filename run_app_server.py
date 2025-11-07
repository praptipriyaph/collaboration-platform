import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ... [gRPC code generation check can stay] ...
proto_dir = os.path.join(os.path.dirname(__file__), 'proto')
if not os.path.exists('service_pb2.py'):
    print("Generating gRPC code from proto file...")
    os.system(f'python -m grpc_tools.protoc -I{proto_dir} --python_out=. --grpc_python_out=. {proto_dir}/service.proto')
    print("gRPC code generated successfully!")

from app_server.server import serve

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python run_app_server.py <node_id> [peer_id_1] [peer_id_2] ...")
        print("Example (Leader): python run_app_server.py localhost:50053 localhost:50054")
        print("Example (Follower 1): python run_app_server.py localhost:50054 localhost:50053")
        sys.exit(1)

    # The first argument is this node's ID
    node_id = sys.argv[1]

    # All other arguments are its peers
    peer_ids = sys.argv[2:]

    print(f"--- LAUNCHING NODE ---")
    print(f"Node ID: {node_id}")
    print(f"Peers:   {peer_ids}")
    print(f"----------------------")

    serve(node_id, peer_ids)