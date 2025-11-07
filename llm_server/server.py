import grpc
from concurrent import futures
import sys
import os
import time
import random

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service_pb2
import service_pb2_grpc


class LLMServiceServicer(service_pb2_grpc.LLMServiceServicer):
    """
    This is the GRPCServicer for the LLM Server.
    It implements the GetLLMAnswer method defined in the .proto file.
    """

    def __init__(self):
        print("LLM Server initialized.")
        self.mock_responses = [
            "Your grammar looks correct.",
            "Summary: The document discusses a new project milestone.",
            "I suggest you rephrase the second paragraph for clarity."
        ]

    def GetLLMAnswer(self, request, context):
        """
        Handles an LLM query from the App Server.
        This is a MOCK implementation.
        """
        print(f"\n[LLM Server] Received query:")
        print(f"  Request ID: {request.request_id}")
        print(f"  Context:    {request.context}")
        print(f"  Query:      {request.query}")

        # Simulate work
        time.sleep(1)

        answer = ""
        # Handle the test connection query from the app_server
        if request.query == "test connection":
            answer = "LLM Server connection successful."
        else:
            answer = f"Mock Answer: {random.choice(self.mock_responses)}"

        print(f"  Sending response: {answer}")

        return service_pb2.LLMResponse(
            request_id=request.request_id,
            answer=answer
        )


def serve():
    """Starts the LLM gRPC server on port 50052."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

    service_pb2_grpc.add_LLMServiceServicer_to_server(
        LLMServiceServicer(), server
    )

    server.add_insecure_port('[::]:50052')
    server.start()
    print("✓ LLM Server started on port 50052.")
    print("Waiting for requests from the App Server...")
    server.wait_for_termination()


if __name__ == '__main__':
    serve()