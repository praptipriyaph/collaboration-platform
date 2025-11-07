import grpc
from concurrent import futures
import sys
import os
import time

from ctransformers import AutoModelForCausalLM

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service_pb2
import service_pb2_grpc

# --- CONFIGURATION (TINYLLAMA) ---
MODEL_DIR = "/Users/nomitachetia/Desktop/PyCharmProjects/collaboration-platform-main/models"
MODEL_FILE = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
MODEL_TYPE = "llama"  # TinyLlama uses the standard llama architecture


class LLMServiceServicer(service_pb2_grpc.LLMServiceServicer):
    def __init__(self):
        print(f"Loading LLM from {MODEL_DIR}/{MODEL_FILE}...")
        try:
            self.llm = AutoModelForCausalLM.from_pretrained(
                MODEL_DIR,
                model_file=MODEL_FILE,
                model_type=MODEL_TYPE,
                gpu_layers=0,
                context_length=2048
            )
            print("✓ LLM successfully loaded and ready!")
        except Exception as e:
            print(f"✗ FAILED to load LLM: {e}")
            print("Please ensure the TinyLlama file is in the 'models' folder.")
            sys.exit(1)

    def GetLLMAnswer(self, request, context_grpc):
        start_time = time.time()
        print(f"\n[LLM Server] Received query: '{request.query}'")

        # TinyLlama Prompt Format:
        # <|system|>\n{system_prompt}</s>\n<|user|>\n{prompt}</s>\n<|assistant|>
        prompt = f"<|system|>\nYou are a helpful assistant for a document collaboration system.\nContext: {request.context}</s>\n<|user|>\n{request.query}</s>\n<|assistant|>"

        print("  Generating response...")
        try:
            response_text = self.llm(
                prompt,
                max_new_tokens=512,
                temperature=0.7,
                stop=["</s>", "<|user|>", "<|system|>"],
                stream=False
            )

            duration = time.time() - start_time
            print(f"  ✓ Response generated in {duration:.2f}s")
            print(f"  Output: {response_text.strip()[:100]}...")

            return service_pb2.LLMResponse(
                request_id=request.request_id,
                answer=response_text.strip()
            )

        except Exception as e:
            error_msg = f"LLM generation failed: {str(e)}"
            print(f"  ✗ {error_msg}")
            return service_pb2.LLMResponse(request_id=request.request_id, answer=error_msg)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    service_pb2_grpc.add_LLMServiceServicer_to_server(LLMServiceServicer(), server)
    server.add_insecure_port('[::]:50052')
    server.start()
    print("✓ LLM Server started on port 50052. Waiting for requests...")
    server.wait_for_termination()


if __name__ == '__main__':
    serve()