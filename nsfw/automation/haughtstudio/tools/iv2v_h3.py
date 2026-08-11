import json
import os
import random
from datetime import datetime

def iv2v(prompt, image_path_list, iv2v_workflow_path, save_directory, video_path=None,
         duration=10.0, megapixels=0.6, aspect_ratio="16:9 (Widescreen)", turboLora=True, ref_quality="match", quant="int8", seed=None, file_name=None):
    """Reference-to-video generation using the MiniMax H3 flex cinematic workflow.

    Args:
        prompt: Text prompt describing the desired video.
        image_path_list: List of up to 4 reference image file paths (character sheets, close-ups, etc.).
        iv2v_workflow_path: Path to the MiniMax H3 workflow JSON.
        save_directory: Directory where the output video/audio should be saved.
        video_path: Optional path to a previous video clip used as a motion/continuation reference.
        duration: Clip length in seconds (converted to frame count by the workflow).
        megapixels: Output resolution budget for the ResolutionSelector node.
        aspect_ratio: Aspect ratio preset for the ResolutionSelector node.
        turboLora: bool to use a lightx2v speed up lora (may reduce quality)
        ref_quality: Quality setting for reference images (default: "match") other value is "max"
        file_name: Optional output filename (without extension).
    """
    print(f"\n\nReceived reference-to-video request for prompt:\n '{prompt}'\n\n\n")

    if not prompt:
        return {'error': 'Prompt is required'}

    if not image_path_list:
        return {'error': 'imagePathList is required'}

    if not isinstance(image_path_list, list):
        return {'error': 'imagePathList must be a list of image file paths'}

    if len(image_path_list) > 4:
        return {'error': 'A maximum of 4 reference images are supported'}

    with open(iv2v_workflow_path, "r") as f:
        workflow = json.load(f)

    

    # ------------------------------------------------------------------
    # Node mapping (minimax_h3_flex_cinematic.json)
    # ------------------------------------------------------------------
    prompt_node = "182"            # PrimitiveStringMultiline - text prompt
    noise_node = "161"             # RandomNoise - noise_seed
    duration_node = "158"          # PrimitiveFloat - clip length in seconds
    resolution_node = "157"        # ResolutionSelector - aspect_ratio / megapixels
    r2v_node = "169"               # MiniMaxH3ReferenceToVideo - main node (refs + prompt)
    save_video_node = "176"        # SaveVideo - final video output
    save_audio_node = "192"        # SaveAudioAdvanced - final audio output
    save_image_node = "210"        # SaveImage - final frame output
    
    bool_turbo_lora_node = "204"  #  - turboLora flag
    turboLora_node = "198"  #  - turboLora node
    step_count_node = "141"
    model_node = "160"
    # The workflow ships with four LoadImage nodes already wired to
    # ref_image_0..3 (178, 170, 187, 203). Do NOT invent new IDs here:
    # node 202 is the MiniMaxH3TurboSampler, so creating a LoadImage at
    # "202" overwrites the sampler and SamplerCustomAdvanced (142) then
    # fails validation with IMAGE -> SAMPLER type mismatch.
    image_input_nodes = ["178", "170", "187", "203"]
    video_input_node = "195"       # VHS_LoadVideoFFmpeg - optional reference clip

    # ------------------------------------------------------------------
    # Core inputs
    # ------------------------------------------------------------------
    if quant not in ["int8", "bf16"]:
        print(f'error: quant must be either "int8" or "bf16", defaulting to "int8"')
        quant = "int8"
    if quant == "int8":
        workflow[model_node]["inputs"]["unet_name"] = "h3\\minimax_h3_fl2va_int8_convrot.safetensors"
    if quant == "bf16":
        workflow[model_node]["inputs"]["unet_name"] = "h3\\minimax_h3_fl2va_bf16.safetensors"

    workflow[prompt_node]["inputs"]["value"] = prompt
    workflow[noise_node]["inputs"]["noise_seed"] = seed if seed is not None else random.randint(1, 1000000000)
    workflow[duration_node]["inputs"]["value"] = duration

    # Resolution selector (only touch if node exists in this workflow)
    if resolution_node in workflow:
        workflow[resolution_node]["inputs"]["aspect_ratio"] = aspect_ratio
        workflow[resolution_node]["inputs"]["megapixels"] = megapixels

    # TurboLora flag (only touch if node exists in this workflow)
    if bool_turbo_lora_node in workflow:
        workflow[bool_turbo_lora_node]["inputs"]["switch"] = turboLora
        if not turboLora:
            #increase to 25 steps if not using speed lora
            workflow[step_count_node]["inputs"]["steps"] = 25
            #set lora strength to 0
            workflow[turboLora_node]["inputs"]["strength_model"] = 0

    #if ref quality
    if ref_quality not in ["match", "max"]:
        print(f'error: ref_quality must be either "match" or "max", defaulting to "match"')
        ref_quality = "match"
    workflow[r2v_node]["inputs"]["ref_image_size"] = ref_quality
    
    # ------------------------------------------------------------------
    # Reference images (up to 4). The workflow ships with four LoadImage
    # nodes pre-wired to ref_image_0..3; any unused slot is pointed back at
    # image 1 so every node stays valid.
    # ------------------------------------------------------------------
    input_files = {}

    num_images = min(len(image_path_list), 4)
    for i in range(num_images):
        placeholder = f"{{{{INPUT_IMAGE{i+1}_PLACEHOLDER}}}}"
        input_files[placeholder] = image_path_list[i]

        if i < len(image_input_nodes):
            load_node = image_input_nodes[i]
            workflow[load_node]["inputs"]["image"] = placeholder
        else:
            # Unreachable: num_images is capped at 4 and the workflow ships
            # with four LoadImage nodes. Kept as a safety net — IDs start at
            # 300 to stay clear of every existing node (202 is the sampler).
            load_node = str(300 + i)
            workflow[load_node] = {
                "inputs": {"image": placeholder},
                "class_type": "LoadImage",
                "_meta": {"title": f"Load Image - {i+1}"}
            }

        # Wire the ref slot on the R2V node to the load node output
        workflow[r2v_node]["inputs"][f"ref_images.ref_image_{i}"] = [load_node, 0]

    # Unused base load nodes get image 1 as a placeholder (single upload,
    # the placeholder is replaced in every location) and their ref slots
    # are wired to those nodes so the R2V inputs stay valid.
    for i in range(num_images, len(image_input_nodes)):
        workflow[image_input_nodes[i]]["inputs"]["image"] = "{{INPUT_IMAGE1_PLACEHOLDER}}"
        workflow[r2v_node]["inputs"][f"ref_images.ref_image_{i}"] = [image_input_nodes[i], 0]

    # ------------------------------------------------------------------
    # Optional reference video (continuation / motion reference)
    # ------------------------------------------------------------------
    if video_path:
        if video_input_node not in workflow:
            return {'error': f'Workflow {os.path.basename(iv2v_workflow_path)} has no video '
                             f'input node ({video_input_node}); cannot use a reference video'}
        video_placeholder = "{{INPUT_VIDEO_PLACEHOLDER}}"
        workflow[video_input_node]["inputs"]["video"] = video_placeholder
        input_files[video_placeholder] = video_path
        workflow[r2v_node]["inputs"]["ref_videos.ref_video_0"] = [video_input_node, 0]
    else:
        # Drop the optional video ref input AND its load node entirely, so a
        # stale hardcoded filename in node 195 can never fail validation.
        workflow[r2v_node]["inputs"].pop("ref_videos.ref_video_0", None)
        workflow.pop(video_input_node, None)

    # ------------------------------------------------------------------
    # Output naming
    # ------------------------------------------------------------------
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if file_name is None or not isinstance(file_name, str) or not file_name.strip():
        if file_name is not None:
            print("Invalid file_name must be a non-empty string with no extension, using default output name.")
        clip_id = f"iv2v_output_{timestamp}.mp4"
        sound_id = f"iv2v_audio_{timestamp}.flac"
        frame_id = f"iv2v_final_frame_{timestamp}.png"
    else:
        clip_id = f"{file_name}.mp4"
        sound_id = f"{file_name}.flac"
        frame_id = f"{file_name}_final_frame.png"

    comfy_request = {
        'workflow': workflow,
        'file_name': clip_id,
        'input_files': input_files,
        'file_prefix': {save_video_node: clip_id, save_audio_node: sound_id, save_image_node: frame_id},
        'save_path': save_directory,
        'node_id': save_video_node,
        'image_node_id': save_image_node,
        'service_type': 'iv2v_h3',
        'prompt_final': prompt
    }

    return comfy_request


if __name__ == "__main__":
    import sys

    # Add parent directory to path to import call_comfyui
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.append(parent_dir)

    from call_comfyui import ComfyUIlocal, WORKFLOWS

    # Initialize ComfyUI client
    comfy_client = ComfyUIlocal() # In testing prevent unloading the model

    # Free VRAM before running
    comfy_client.aggressive_cleanup()

    # Testing files
    test_image_1 = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\apt_kitchen.png"
    test_image_2 = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\apt_livingroom.png"
    test_3xCharSheet = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\fiona\charSheet_x3_1.png"
    test_video = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\fiona\outputs\r2v_00001_.mp4"

    output_directory = "test/output"
    workflow_file = "comfy_workflows/minimax_h3_flex_cinematic.json"

    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, output_directory)
    WORKFLOWS['iv2v_h3'] = os.path.join(parent_dir, workflow_file)

    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)

    ############################### 2 Images + Video ###############################
    print("\n\nTESTING 2 Image Inputs + Video\n\n")
    test_prompt = "The subject walks through a neon-lit city street at night, cinematic lighting, shallow depth of field"
    test_image_path_list = [test_3xCharSheet, test_image_1]

    request_data = iv2v(
        prompt=test_prompt,
        image_path_list=test_image_path_list,
        video_path=test_video,
        iv2v_workflow_path=WORKFLOWS['iv2v_h3'],
        save_directory=test_save_dir,
        duration=5.0
    )

    if 'error' in request_data:
        print(f"Error building request: {request_data['error']}")
        sys.exit(1)

    print("Executing workflow...")
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type=request_data['service_type'],
        input_files=request_data['input_files'],
        file_prefix=request_data['file_prefix'],
        output_paths={request_data['node_id']: os.path.join(request_data['save_path'], request_data['file_name'])}
    )

    if result.get('files'):
        print(f"Success! Output saved to: {os.path.join(request_data['save_path'], request_data['file_name'])}")
    else:
        print("Failed to generate video:", result.get('error', 'Unknown error'))
