import json
import os
import random
from datetime import datetime


def t2i_sdxl(prompt,negative_prompt, enhance_prompt,t2i_workflow_path,save_directory,file_name=None):
    """Generate an image from text. this prepares the workflow to be sent to comfyui."""
    print(f"\n\nReceived image generation request for prompt:\n '{prompt}'\n\n\n")
    
    if not prompt:
        return {'error': 'Prompt is required'}
    
    
    #Get the workflow for this endpoint. For now we can hardcode it, but eventually we may want to allow users to specify different workflows for different endpoints.
    with open(t2i_workflow_path, "r") as f:
        workflow = json.load(f)

    positive_prompt_node = "58"
    negative_prompt_node = "7"
    enhance_prompt_node = "109"
    save_node = "91"

    #noise seed
    workflow["13"]["inputs"]["noise_seed"] = random.randint(1, 1000000000)
    #text pompt input
    #SDXL text encoder uses an 'L' and a 'G' input, where 'L' is used as a seconday prompt for style. For now we will just use the same prompt for both, but eventually we may want to allow users to specify a separate style prompt.
    workflow[positive_prompt_node]["inputs"]["text_g"] = prompt
    workflow[positive_prompt_node]["inputs"]["text_l"] = prompt 
    workflow[negative_prompt_node]["inputs"]["text"] = negative_prompt or "text, watermark, realistic photo, 3d render, gradient background, cluttered, busy, low quality, blurry, sketch, hand drawn, cartoon, animation"
    workflow[enhance_prompt_node]["inputs"]["text"] =  enhance_prompt or "cinematic photograph, high contrast, professional photo, sharp focus, shallow depth of field"

    #DISNEY AND PIXAR STYLE BOOST
    if "pixar" in prompt.lower() or "3d animation" in prompt.lower():
        #Disney and Pixar LoRA
        workflow["121"]["inputs"]["strength_model"] = 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    base_image = f'sdxl_output_{timestamp}'
    #change if we got a real file name from the character
    if file_name:
        base_image = file_name
    workflow[save_node]["inputs"]["filename_prefix"] = base_image

    #save path
    save_path = os.path.join(save_directory, f'{base_image}.png')
    
    # Forward to image generation service
    comfy_request = {'workflow': workflow, 'file_name': base_image, 'save_path': save_path, 'node_id': save_node}

    return comfy_request

if __name__ == "__main__":
    import sys
    
    # Add parent directory to path to import call_comfyui
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.append(parent_dir)
    
    from call_comfyui import ComfyUIlocal, WORKFLOWS
    
    test_prompt = "A close-up cinematic portrait of a young woman with wavy auburn hair and pale skin, deep green eyes, soft lighting, shallow depth of field"
    test_negative_prompt = "blurry, low resolution, bad quality, cartoon, 3d render"
    test_enhance_prompt = "cinematic photograph, high contrast, professional photo, sharp focus"
    # Using one of the cinematic workflows available in your comfy_workflows folder
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, "test")
    
    # Resolve workflow path relative to parent directory (project root)
    WORKFLOWS['t2i_sdxl_cinematic'] = os.path.join(parent_dir, WORKFLOWS['t2i_sdxl_cinematic'])
    
    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)
        
    print(f"Testing t2i integration. Saving to {test_save_dir}")
    
    request_data = t2i_sdxl(
        prompt=test_prompt,
        negative_prompt=test_negative_prompt,
        enhance_prompt=test_enhance_prompt,
        t2i_workflow_path=WORKFLOWS['t2i_sdxl_cinematic'],
        save_directory=test_save_dir,
        file_name="test_sdxl_cinematic"
    )
    
    # Initialize ComfyUI client (default port 8188 for ComfyUI, update if your server uses port 8000)
    comfy_client = ComfyUIlocal()

    #CALL COMFYUI VRAM CLEAN HERE
    comfy_client.aggressive_cleanup()
    
    print("Executing workflow...")
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type='t2i_sdxl_cinematic',
        output_paths={request_data['node_id']: request_data['save_path']}
    )
    
    if result.get('files'):
        print(f"Success! Output saved to: {request_data['save_path']}")
    else:
        print("Failed to generate image:", result.get('error', 'Unknown error'))
    

   