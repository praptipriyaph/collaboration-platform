import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Generate gRPC code if not exists
proto_dir = os.path.join(os.path.dirname(__file__), 'proto')
if not os.path.exists('service_pb2.py'):
    print("Generating gRPC code from proto file...")
    os.system(f'python -m grpc_tools.protoc -I{proto_dir} --python_out=. --grpc_python_out=. {proto_dir}/service.proto')
    print("gRPC code generated successfully!")

from llm_server.server import serve

if __name__ == '__main__':
    serve()