import json
import os
import random
from datetime import datetime

def v2v(prompt, image_path_list, video_path, v2v_workflow_path, save_directory,
        duration=12.0, megapixels=0.6, aspect_ratio="16:9 (Widescreen)", turboLora=True,
        ref_quality="match", quant="int8", seed=None, file_name=None, context_frames=None):
    """Video-to-video generation using the MiniMax H3 v2v cinematic workflow.

    Args:
        prompt: Text prompt describing the desired video.
        image_path_list: List of up to 6 reference image file paths (character sheets, close-ups, etc.).
        video_path: Path to the source video clip to extend / transform (required).
        v2v_workflow_path: Path to the MiniMax H3 v2v workflow JSON.
        save_directory: Directory where the output video should be saved.
        duration: New clip length in seconds (converted to frame count by the workflow).
        megapixels: Output resolution budget for the ResolutionSelector node.
        aspect_ratio: Aspect ratio preset for the ResolutionSelector node.
        turboLora: bool to use the turbo 8-step speed lora (may reduce quality)
        ref_quality: Quality setting for reference images (default: "match") other value is "max"
        quant: Model quantization, either "int8" or "bf16".
        seed: Optional noise seed; randomized when None.
        file_name: Optional output filename (without extension).
        context_frames: Optional number of protected source frames used as the AV prefix
                        (defaults to the workflow's built-in value of 39).
    """
    print(f"\n\nReceived video-to-video request for prompt:\n '{prompt}'\n\n\n")

    if not prompt:
        return {'error': 'Prompt is required'}

    if not image_path_list:
        return {'error': 'imagePathList is required'}

    if not isinstance(image_path_list, list):
        return {'error': 'imagePathList must be a list of image file paths'}

    if len(image_path_list) > 6:
        return {'error': 'A maximum of 6 reference images are supported'}

    if not video_path:
        return {'error': 'video_path is required for video-to-video generation'}

    with open(v2v_workflow_path, "r") as f:
        workflow = json.load(f)



    # ------------------------------------------------------------------
    # Node mapping (minimax_h3_v2v_cinematic.json)
    # ------------------------------------------------------------------
    prompt_node = "80"             # PrimitiveStringMultiline - text prompt
    noise_node = "25"              # RandomNoise - noise_seed
    duration_node = "63"           # PrimitiveFloat - new clip length in seconds
    r2v_node = "23"                # MiniMaxH3ReferenceToVideo - main node (refs + prompt)
    resolution_node = "74"         # load - width/height settings for resolution
    save_video_node = "43"         # VHS_VideoCombine - extension video output
    assemble_node = "85"           # MiniMaxH3AssembleExtensionCheckpoints - source + extension output
    save_image_node = "90"        # SaveImage - final frame output
    save_audio_node = "192"        # SaveAudioAdvanced - final audio output    
    sampler_node = "68"          # MiniMaxH3TurboSampler - sample 'euler' on turbo and 'res_multistep' on non-turbo
    scheduler_node = "27"         # MiniMaxH3 Scheduler - scheduler 'beta' on turbo and 'simple' on non-turbo
    switch_shift_node = "99"        # MiniMaxH3Shift - shift for audio/video alignment
    turboLora_node = "7"           # LoraLoaderModelOnly - turbo 8-step lora
    step_count_node = "27"         # BasicScheduler - step count
    model_node = "2"               # UNETLoader - H3 ref2va model
    context_frames_node = "13"     # PrimitiveInt - global context frames
    nsfw_lora_node = "98"          # NSFW lora (always enabled, strength is 0.4) dial up to 0.75
    # The workflow ships with four LoadImage nodes already wired to
    # ref_image_0..3 (19, 20, 62, 76). Do NOT invent new IDs here.
    image_input_nodes = ["19", "20", "62", "76", "94", "95"]  # LoadImage nodes for ref_image_0..5
    video_input_node = "88"         # VHS_LoadVideo - source video (required)

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
    if megapixels <= 0.2:
        width, height = 608, 352
    elif megapixels <= 0.3:
        width, height = 736, 416
    elif megapixels <= 0.4:
        width, height = 864, 480
    elif megapixels <= 0.5:
        width, height = 960, 544
    elif megapixels <= 0.6:
        width, height = 1056, 608
    elif megapixels <= 0.7:
        width, height = 1152, 640
    elif megapixels <= 0.8:
        width, height = 1216, 672
    elif megapixels <= 0.9:
        width, height = 1280, 736
    else:
        width, height = 1344, 768  # 0.98+ max resolution H3 trained on

    if resolution_node in workflow:
        workflow[resolution_node]["inputs"]["width"] = width
        workflow[resolution_node]["inputs"]["height"] = height

    # Protected source frames used as the AV prefix (only touch if requested)
    if context_frames is not None and context_frames_node in workflow:
        workflow[context_frames_node]["inputs"]["value"] = context_frames

    # TurboLora flag. Unlike the iv2v workflow there is no boolean switch
    # node here; the lora sits directly in the model chain, so disabling it
    # means zeroing its strength and raising the step count.
    if turboLora_node in workflow:
        if not turboLora:
            #steps
            workflow[step_count_node]["inputs"]["steps"] = 40
            #set lora strength to 0
            workflow[turboLora_node]["inputs"]["strength_model"] = 0
            #sampler
            workflow[sampler_node]["inputs"]["sampler_name"] = "res_multistep"
            #scheduler
            workflow[scheduler_node]["inputs"]["scheduler"] = "simple"
            #adjust  shift
            workflow[switch_shift_node]["inputs"]["switch"] = False
            
        else:
            #steps
            workflow[step_count_node]["inputs"]["steps"] = 8
            #set lora strength to 1
            workflow[turboLora_node]["inputs"]["strength_model"] = 1
            #sampler
            workflow[sampler_node]["inputs"]["sampler_name"] = "euler"
            #scheduler
            workflow[scheduler_node]["inputs"]["scheduler"] = "beta"
            #adjust  shift
            workflow[switch_shift_node]["inputs"]["switch"] = True
            #Adjust which LoRA based on megapixels
            if megapixels <= 0.6:
                workflow[turboLora_node]["inputs"]["lora_name"] = "H3\\minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
            else:
                workflow[turboLora_node]["inputs"]["lora_name"] = "H3\\minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"
            

    if 'nsfw' in prompt.lower():
        workflow[nsfw_lora_node]["inputs"]["strength_model"] = 0.7
    else:
        workflow[nsfw_lora_node]["inputs"]["strength_model"] = 0

    #if ref quality
    if ref_quality not in ["match", "max"]:
        print(f'error: ref_quality must be either "match" or "max", defaulting to "match"')
        ref_quality = "match"
    workflow[r2v_node]["inputs"]["ref_image_size"] = ref_quality

    # ------------------------------------------------------------------
    # Reference images (up to 6). The workflow ships with four LoadImage
    # nodes pre-wired to ref_image_0..5 any unused slot is pointed back at
    # image 1 so every node stays valid.
    # ------------------------------------------------------------------
    input_files = {}

    num_images = min(len(image_path_list), 6)
    for i in range(num_images):
        placeholder = f"{{{{INPUT_IMAGE{i+1}_PLACEHOLDER}}}}"
        input_files[placeholder] = image_path_list[i]

        if i < len(image_input_nodes):
            load_node = image_input_nodes[i]
            workflow[load_node]["inputs"]["image"] = placeholder
        else:
            # Unreachable: num_images is capped at 6 and the workflow ships
            # with four LoadImage nodes. Kept as a safety net — IDs start at
            # 300 to stay clear of every existing node.
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
    # Source video (required). Unlike iv2v, the clip is NOT wired to a
    # ref_videos slot on the R2V node — it feeds the StartMaskedContext
    # node (24) as source_frames/source_audio via the load node only.
    # ------------------------------------------------------------------
    if video_input_node not in workflow:
        return {'error': f'Workflow {os.path.basename(v2v_workflow_path)} has no video '
                         f'input node ({video_input_node}); cannot run video-to-video'}
    video_placeholder = "{{INPUT_VIDEO_PLACEHOLDER}}"
    workflow[video_input_node]["inputs"]["video"] = video_placeholder
    input_files[video_placeholder] = video_path

    # ------------------------------------------------------------------
    # Output naming
    # ------------------------------------------------------------------
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if file_name is None or not isinstance(file_name, str) or not file_name.strip():
        if file_name is not None:
            print("Invalid file_name must be a non-empty string with no extension, using default output name.")
        clip_id = f"v2v_output_{timestamp}.mp4"
        assembled_id = f"v2v_assembled_{timestamp}.mp4"
        frame_id = f"v2v_final_frame_{timestamp}.png"
        sound_id = f"iv2v_audio_{timestamp}.flac"
    else:
        clip_id = f"{file_name}.mp4"
        assembled_id = f"{file_name}_assembled.mp4"
        frame_id = f"{file_name}_final_frame.png"
        sound_id = f"{file_name}_audio.flac"

    comfy_request = {
        'workflow': workflow,
        'file_name': clip_id,
        'input_files': input_files,
        'file_prefix': {save_video_node: clip_id, assemble_node: assembled_id, save_image_node: frame_id, save_audio_node: sound_id},
        'save_path': save_directory,
        'node_id': save_video_node,
        'assemble_node_id': assemble_node,
        'image_node_id': save_image_node,
        'audio_node_id': save_audio_node,
        'service_type': 'v2v_h3',
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
    comfy_client = ComfyUIlocal(last_workflow='v2v_h3') # In testing prevent unloading the model

    # Free VRAM before running
    comfy_client.aggressive_cleanup()

    # Testing files
    test_image_1 = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\apt_kitchen.png"
    test_image_2 = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\apt_livingroom.png"
    test_3xCharSheet = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\fiona\charSheet_x3_1.png"
    test_video = r"C:\Users\jared\Documents\code\haughtstudio\automation\luxuryAptEncounter\fiona\outputs\r2v_00001_.mp4"

    output_directory = "test/output"
    workflow_file = "comfy_workflows/minimax_h3_v2v_cinematic.json"

    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, output_directory)
    WORKFLOWS['v2v_h3'] = os.path.join(parent_dir, workflow_file)

    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)

    ############################### 2 Images + Source Video ###############################
    print("\n\nTESTING 2 Image Inputs + Source Video\n\n")
    test_prompt = """How the reference pictures align with the target video there is no starting frame; the 0.00-second mark is the last frame of <Video 1> Continue the exact motion already in progress, she is asleep in bed. Target duration 10.00 seconds.

[Shot 1] Continue the exact motion in progress, then develop the next action naturally.

summary:[reference generation] The target video composes its opening from the combined references and shows <Subject 1> waking up in a hotel room, getting out of bed to get clothing.

subject_definitions:<Subject 1> is the woman defined by <Picture 1>, <Picture 2>, she has vibrant red chin-length bob hair, light skin with prominent freckles, and green eyes. She wears large gold hoop earrings and a gold curb-chain necklace, and is shown topless. Her identity follows <Picture 1> close up face shot, and <Picture 2> three panel multi-view body; her position and lighting are those of the target room, starting asleep in bed.

<Subject 2> is the room from <Video 1> and <Picture 3>: a hotel-style bedroom with dark wood paneling, a large bed with grey and white linens, nightstands with glowing lamps, and a blue sofa in the foreground. The room follows <Picture 3> in layout, materials, and light; camera position and framing follow the action and are not constrained to the plate's viewpoint.

integrated_multimodal_description: [Shot 1] Live-action, cinematic, dim morning light from lamps. The shot opens with <Subject 1> in bed, the room is silent, her arm is reached over to the alarm clock on her nightstand. <Subject 1> (S1) mumbles in a soft New York accent, <d>[English] ugh.. one more day and I'm done.</d> she pulls her arm back and tosses the blankets aside and gets out of bed. she stands up, the Camera rotates around her to show her from behind looking at her closet of clothing she walks over to the closet and rummages through the hanging clothing with her hands looking for a shirt to wear.

non_diegetic_music: N/A"""
    test_image_path_list = [test_3xCharSheet, test_image_1]

    request_data = v2v(
        prompt=test_prompt,
        image_path_list=test_image_path_list,
        video_path=test_video,
        v2v_workflow_path=WORKFLOWS['v2v_h3'],
        save_directory=test_save_dir,
        duration=5.0
    )

    if 'error' in request_data:
        print(f"Error building request: {request_data['error']}")
        sys.exit(1)

    print("Executing workflow...")
    assembled_file_name = request_data['file_prefix'][request_data['assemble_node_id']]
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type=request_data['service_type'],
        input_files=request_data['input_files'],
        file_prefix=request_data['file_prefix'],
        output_paths={
            # Extension clip (node 43) and assembled source+extension video (node 85)
            request_data['node_id']: os.path.join(request_data['save_path'], request_data['file_name']),
            request_data['assemble_node_id']: os.path.join(request_data['save_path'], assembled_file_name)
        }
    )

    if result.get('files'):
        print(f"Success! Extension clip saved to: {os.path.join(request_data['save_path'], request_data['file_name'])}")
        print(f"Success! Assembled video saved to: {os.path.join(request_data['save_path'], assembled_file_name)}")
    else:
        print("Failed to generate video:", result.get('error', 'Unknown error'))
