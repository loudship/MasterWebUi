import os
import time
import base64
import io
import requests
from PIL import Image
import mss

# --- Configuration ---
# Route through the inference gateway (host port 4322), NOT LM Studio directly:
# direct calls bypass the model allowlist and the GPU serialization lock, so
# vision prefill interleaved with chat token generation and caused periodic
# stutter on every stream (audit P1-4).
VISION_ENDPOINT = os.environ.get(
    "VISION_ENDPOINT", "http://127.0.0.1:4322/v1/chat/completions"
)
MODEL_NAME = os.environ.get("VISION_MODEL", "qwen2-vl-4b-instruct")  # must be allowlisted
MAX_IMAGE_DIM = 1024  # Maximum dimension for resizing
# 30 s default: a 10 s cadence kept the vision model permanently resident in
# the 12 GB VRAM budget alongside the chat model.
POLL_INTERVAL = float(os.environ.get("VISION_POLL_INTERVAL_S", "30"))

def capture_and_encode():
    """Captures the primary screen, resizes/compresses it, and encodes to base64."""
    with mss.mss() as sct:
        # Grab the primary monitor (index 1 is primary, 0 is all monitors combined)
        monitor = sct.monitors[1]
        screenshot = sct.grab(monitor)
        
        # Convert mss raw pixels to a PIL Image
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        
        # --- Base64 Compression & Resize Logic ---
        # 1. Resize: Check if image exceeds MAX_IMAGE_DIM to prevent massive base64 strings
        #    which cause token bloat and slow down local inference.
        width, height = img.size
        if width > MAX_IMAGE_DIM or height > MAX_IMAGE_DIM:
            # Maintain aspect ratio
            if width > height:
                new_width = MAX_IMAGE_DIM
                new_height = int(MAX_IMAGE_DIM * (height / width))
            else:
                new_height = MAX_IMAGE_DIM
                new_width = int(MAX_IMAGE_DIM * (width / height))
            
            # Apply high-quality downsampling
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
        # 2. Compress: Save to an in-memory buffer as JPEG (lossy compression) 
        #    instead of PNG to drastically reduce the byte size.
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=80)
        
        # 3. Base64 Encode: Convert byte array to standard base64 string required by OpenAI-compatible endpoints
        img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
        
        # Prepend the data URI scheme so it's recognized as a valid image URL payload
        return f"data:image/jpeg;base64,{img_str}"

def analyze_screen(prompt="What is currently on my screen? Are there any alerts or errors?"):
    try:
        print("Capturing frame...")
        base64_image = capture_and_encode()
        
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": base64_image
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 300,
            "temperature": 0.2
        }
        
        print(f"Sending frame to Vision Model at {VISION_ENDPOINT}...")
        response = requests.post(VISION_ENDPOINT, json=payload, timeout=30)
        if response.status_code == 503:
            # Gateway is protecting an interactive generation — yield the GPU.
            print("Gateway busy with an interactive generation — skipping this cycle.")
            return
        response.raise_for_status()
        
        result = response.json()
        print("\n--- Visual Cortex Analysis ---")
        print(result["choices"][0]["message"]["content"])
        print("------------------------------\n")
        
    except requests.exceptions.Timeout:
        print("Error: Vision Model API request timed out after 30 seconds.")
    except requests.exceptions.ConnectionError:
        print("Error: Connection to Vision Model API failed.")
    except Exception as e:
        print(f"Error during vision analysis: {e}")

if __name__ == "__main__":
    print("========================================")
    print("Ghost Command Visual Cortex Daemon")
    print(f"Target Vision Endpoint: {VISION_ENDPOINT}")
    print("========================================")
    print("Starting polling loop... Press Ctrl+C to exit.")
    
    try:
        while True:
            analyze_screen()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nVisual Cortex daemon terminated.")
