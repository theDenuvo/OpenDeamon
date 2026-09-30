import torch
from diffusers import StableDiffusionPipeline
import os
import json
import sys

prompt = "a realistic cat sitting on a white windowsill, sunlight, 3:4 aspect ratio, photorealistic, 8k, detailed"
width = 300
height = 400
output_path = "A:/OpenDeamon/cat_local2.png"

def generate_with_model(model_id):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device} for model {model_id}", file=sys.stderr)
    # Load pipeline with safety checker disabled for speed
    pipe = StableDiffusionPipeline.from_pretrained(
        model_id, 
        torch_dtype=torch.float16 if device == "cuda" else torch.float32,
        safety_checker=None
    )
    pipe = pipe.to(device)
    # Enable attention slicing for lower memory usage if needed
    pipe.enable_attention_slicing()
    # Generate image with reduced steps for speed
    image = pipe(prompt, width=width, height=height, num_inference_steps=25).images[0]
    return image

try:
    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    # Try primary model
    try:
        image = generate_with_model("runwayml/stable-diffusion-v1-5")
    except Exception as e:
        print(f"Primary model failed: {e}. Trying CompVis/stable-diffusion-v1-4", file=sys.stderr)
        image = generate_with_model("CompVis/stable-diffusion-v1-4")
    
    # Save image
    image.save(output_path)
    print(f"Image saved to {output_path}", file=sys.stderr)
    result = {"status": "success", "image_path": output_path, "error": ""}
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    result = {"status": "failure", "image_path": "", "error": str(e)}

# Output result as JSON to stdout
print(json.dumps(result))