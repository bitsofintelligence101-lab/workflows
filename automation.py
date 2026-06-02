import json
import os
import random
from datetime import datetime
import re
from character_poses import pose_prompts
from call_comfyui import ComfyUIlocal, WORKFLOWS
from ffmpeg_tools import get_unique_filename, trim_and_join_clips


def get_pose_prompt(category: str, pose_id: str) -> dict:
    """
    Get a specific pose prompt by category and ID.
    
    Args:
        category: One of 'background_variation', 'lighting_variation', 
                  'facial_angle_variation', 'expression_variation', 'action_pose_variation'
        pose_id: The pose number as a string (e.g., '1', '2', '3')
    
    Returns:
        Dict with pose_prompt, category, and image_count
    """
    poses = pose_prompts()
    if category not in poses:
        raise ValueError(f"Invalid category: {category}. Valid options: {list(poses.keys())}")
    
    if pose_id not in poses[category]:
        raise ValueError(f"Invalid pose_id: {pose_id} for category {category}. Valid options: {list(poses[category].keys())}")
    
    return poses[category][pose_id]


def list_available_poses():
    """Print all available pose categories and their IDs."""
    poses = pose_prompts()
    print("\nAvailable Pose Categories and IDs:")
    print("=" * 50)
    for category, pose_dict in poses.items():
        print(f"\n{category}:")
        for pose_id, pose_data in pose_dict.items():
            # Truncate prompt for display
            prompt_preview = pose_data['pose_prompt'][:60] + "..." if len(pose_data['pose_prompt']) > 60 else pose_data['pose_prompt']
            print(f"  {pose_id}: {prompt_preview}")


def generate_pose(
    image_path: str,
    category: str,
    pose_id: str,
    save_directory: str,
    workflow_path: str = None,
    width: int = 1280,
    height: int = 720,
    comfyui_ip: str = "127.0.0.1",
    port: int = 8000,
    reference_image_path: str = None,
    comfy_client: ComfyUIlocal = None
) -> dict:
    """
    Generate a pose variation using ComfyUI.
    
    Args:
        image_path: Path to the input image
        category: Pose category (e.g., 'background_variation', 'expression_variation')
        pose_id: Pose ID within the category (e.g., '1', '2')
        save_directory: Directory to save the output image
        workflow_path: Path to the ComfyUI workflow JSON (defaults to i2i workflow)
        width: Output image width (default 1280)
        height: Output image height (default 720)
        comfyui_ip: ComfyUI server IP address
        port: ComfyUI server port
        reference_image_path: Optional second reference image for poses that require 2 images
    
    Returns:
        Dict with generation results including saved file paths
    """
    # Validate inputs
    if not os.path.exists(image_path):
        return {'error': f'Image not found: {image_path}'}
    
    # Get the pose prompt
    try:
        pose_data = get_pose_prompt(category, pose_id)
    except ValueError as e:
        return {'error': str(e)}
    
    prompt = pose_data['pose_prompt']
    image_count = pose_data['image_count']
    
    # Check if this pose requires a reference image
    if image_count == 2 and not reference_image_path:
        return {'error': f'This pose ({category}/{pose_id}) requires a reference image. Provide reference_image_path.'}
    
    if reference_image_path and not os.path.exists(reference_image_path):
        return {'error': f'Reference image not found: {reference_image_path}'}
    
    # Use default workflow if not specified
    if workflow_path is None:
        workflow_path = WORKFLOWS.get("i2i")
    
    if not os.path.exists(workflow_path):
        return {'error': f'Workflow file not found: {workflow_path}'}
    
    # Load workflow
    with open(workflow_path, "r") as f:
        workflow = json.load(f)
    
    # Configure workflow
    #print("ADDING PIXAR ENHANCEMENT")
    #workflow["105"]["inputs"]["prompt"] = prompt + " 3d pixar animation"
    workflow["105"]["inputs"]["prompt"] = prompt

    #workflow["241"]["inputs"]["width"] = width
    #workflow["241"]["inputs"]["height"] = height
    workflow["41"]["inputs"]["image"] = "{{INPUT_IMAGE_PLACEHOLDER}}"
    
    # Node IDs for outputs
    save_node = "217"
    
    
    # Generate unique filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"{category}_{pose_id}_{timestamp}.png"
    
    # Prepare input files
    input_files = {"{{INPUT_IMAGE_PLACEHOLDER}}": image_path}
    
    # Add reference image if needed (for 2-image poses)
    if image_count == 2 and reference_image_path:
        workflow["42"]["inputs"]["image"] = "{{REFERENCE_IMAGE_PLACEHOLDER}}"  # Adjust node ID as needed
        input_files["{{REFERENCE_IMAGE_PLACEHOLDER}}"] = reference_image_path
    
    # Ensure save directory exists
    os.makedirs(save_directory, exist_ok=True)
    
    # Initialize ComfyUI client
    _owns_client = comfy_client is None
    if _owns_client:
        print(f"Connecting to ComfyUI at {comfyui_ip}:{port}...")
        comfy_client = ComfyUIlocal(
            comfyui_ip=comfyui_ip,
            port=port,
            output_dir=save_directory,
            workflow_type='i2i'
        )

    try:
        # Execute workflow
        result = comfy_client.generate(
            workflow=workflow,
            service_type='i2i',
            input_files=input_files,
            file_prefix={save_node: output_filename},
            output_paths={save_node: os.path.join(save_directory, output_filename)}
        )
        
        return {
            'success': True,
            'category': category,
            'pose_id': pose_id,
            'prompt': prompt,
            'output': result
        }

    except Exception as e:
        return {'error': str(e)}

    finally:
        if _owns_client:
            comfy_client.close()


def generate_pose_2image(
    image_path: str,
    reference_image_path: str,
    category: str,
    pose_id: str,
    save_directory: str,
    workflow_path: str = None,
    width: int = 1280,
    height: int = 720,
    comfyui_ip: str = "127.0.0.1",
    port: int = 8002,
    comfy_client: ComfyUIlocal = None,
) -> dict:
    """
    Generate a pose variation using ComfyUI with two image inputs.
    Node 41 receives the character image; node 129 receives the reference/background image.
    """
    if not os.path.exists(image_path):
        return {'error': f'Image not found: {image_path}'}
    if not os.path.exists(reference_image_path):
        return {'error': f'Reference image not found: {reference_image_path}'}

    try:
        pose_data = get_pose_prompt(category, pose_id)
    except ValueError as e:
        return {'error': str(e)}

    prompt = pose_data['pose_prompt']

    if workflow_path is None:
        workflow_path = WORKFLOWS.get("i2b") #USE A BACKGROUND REPLACEMENT WORKFLOW FOR 2-IMAGE POSES

    if not os.path.exists(workflow_path):
        return {'error': f'Workflow file not found: {workflow_path}'}

    with open(workflow_path, "r") as f:
        workflow = json.load(f)

    workflow["105"]["inputs"]["prompt"] = prompt
    workflow["41"]["inputs"]["image"] = "{{INPUT_IMAGE_PLACEHOLDER}}"
    workflow["129"]["inputs"]["image"] = "{{REFERENCE_IMAGE_PLACEHOLDER}}"

    save_node = "155"

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"{category}_{pose_id}_{timestamp}.png"

    input_files = {
        "{{INPUT_IMAGE_PLACEHOLDER}}": image_path,
        "{{REFERENCE_IMAGE_PLACEHOLDER}}": reference_image_path,
    }

    os.makedirs(save_directory, exist_ok=True)

    _owns_client = comfy_client is None
    if _owns_client:
        comfy_client = ComfyUIlocal(
            comfyui_ip=comfyui_ip,
            port=port,
            output_dir=save_directory
        )

    try:
        result = comfy_client.generate(
            workflow=workflow,
            service_type='i2i',
            input_files=input_files,
            file_prefix={save_node: output_filename},
            output_paths={save_node: os.path.join(save_directory, output_filename)}
        )

        return {
            'success': True,
            'category': category,
            'pose_id': pose_id,
            'prompt': prompt,
            'output': result
        }

    except Exception as e:
        return {'error': str(e)}

    finally:
        if _owns_client:
            comfy_client.close()


def generate_t2i(
    image_prompt: str,
    save_directory: str,
    negative_prompt: str = "",
    enhance_prompt: str = "",
    workflow_path: str = None,
    width: int = 1472,
    height: int = 1104,
    comfyui_ip: str = "127.0.0.1",
    port: int = 8000,
    comfy_client: ComfyUIlocal = None,
) -> dict:
    """
    Generate an image from text using the t2i workflow (Flesh4Fantasy / SDXL).

    Args:
        image_prompt: Main text prompt (used for both SDXL G and L clip inputs)
        save_directory: Directory to save the output image
        negative_prompt: Negative conditioning text
        enhance_prompt: Prompt fed to the ZIT enhancer node
        workflow_path: Path to the ComfyUI workflow JSON (defaults to t2i workflow)
        width: Output width (default 1472)
        height: Output height (default 1104)
        comfyui_ip: ComfyUI server IP address
        port: ComfyUI server port

    Returns:
        Dict with generation results including saved file paths
    """
    # Node IDs
    inputNodes = "58"
    negativeInputNodes = "7"
    enhanceNodes = "109"
    noiseNodes = "13"
    widthNode = "92"
    heightNode = "93"
    eWidthHeightNode = "95"
    saveNodeRaw = "91"
    saveNodeUpscale = "135"

    if workflow_path is None:
        workflow_path = WORKFLOWS.get("t2i")

    if not os.path.exists(workflow_path):
        return {'error': f'Workflow file not found: {workflow_path}'}

    with open(workflow_path, "r") as f:
        workflow = json.load(f)

    # Noise seed
    seed = random.randint(1, 1_000_000_000)
    workflow[noiseNodes]["inputs"]["noise_seed"] = seed

    # Text prompt (SDXL uses both G and L clip inputs)
    workflow[inputNodes]["inputs"]["text_g"] = image_prompt
    workflow[inputNodes]["inputs"]["text_l"] = image_prompt
    workflow[negativeInputNodes]["inputs"]["text"] = negative_prompt
    workflow[enhanceNodes]["inputs"]["text"] = enhance_prompt

    # Output dimensions
    #workflow[widthNode]["inputs"]["value"] = width
    #workflow[heightNode]["inputs"]["value"] = height
    #workflow[eWidthHeightNode]["inputs"]["width"] = int(1.15 * width)
    #workflow[eWidthHeightNode]["inputs"]["height"] = int(1.15 * height)

    # Generate unique filenames for both save nodes
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_filename = f"t2i_raw_{timestamp}.png"
    upscale_filename = f"t2i_upscale_{timestamp}.png"

    os.makedirs(save_directory, exist_ok=True)

    _owns_client = comfy_client is None
    if _owns_client:
        print(f"Connecting to ComfyUI at {comfyui_ip}:{port}...")
        comfy_client = ComfyUIlocal(
            comfyui_ip=comfyui_ip,
            port=port,
            output_dir=save_directory,
            workflow_type='t2i'
        )

    try:
        result = comfy_client.generate(
            workflow=workflow,
            service_type='t2i',
            file_prefix={
                saveNodeRaw: raw_filename,
                saveNodeUpscale: upscale_filename,
            },
            output_paths={
                saveNodeRaw: os.path.join(save_directory, raw_filename),
                saveNodeUpscale: os.path.join(save_directory, upscale_filename),
            }
        )

        return {
            'success': True,
            'prompt': image_prompt,
            'seed': seed,
            'output': result
        }

    except Exception as e:
        return {'error': str(e)}

    finally:
        if _owns_client:
            comfy_client.close()


def generate_i2v(
    image_path: str,
    save_directory: str,
    prompt: str = "",
    negative_prompt: str = "pc game, cgi, 3d render, console game, video game, cartoon, childish, ugly, skin acne, skin pimples, blemish, birthmark",
    workflow_path: str = None,
    width: int = 1280,
    height: int = 768,
    num_frames: int = 360,
    comfyui_ip: str = "127.0.0.1",
    port: int = 8000,
    comfy_client: ComfyUIlocal = None,
) -> dict:
    """
    Generate a video from an input image using the i2v workflow (LTX 2.3).

    Args:
        image_path: Path to the input reference image
        save_directory: Directory to save the output video and last-frame image
        prompt: Text prompt describing the motion/scene
        negative_prompt: Negative conditioning text
        workflow_path: Path to the ComfyUI workflow JSON (defaults to i2v workflow)
        width: Output width (default 1280)
        height: Output height (default 768)
        num_frames: Number of frames to generate at 24 fps (default 360 = 15 s)
        comfyui_ip: ComfyUI server IP address
        port: ComfyUI server port

    Returns:
        Dict with generation results including saved file paths
    """
    # Node IDs
    imageNode      = "269"   # LoadImage
    promptNode     = "303"   # CLIP Text Encode (Prompt)
    negativeNode   = "313"   # CLIP Text Encode (NEGATIVE Prompt)
    seedNode       = "276"   # RandomNoise
    numFramesNode  = "301"   # PrimitiveInt – Length
    widthNode      = "312"   # PrimitiveInt – Width
    heightNode     = "299"   # PrimitiveInt – Height
    saveImageNode  = "334"   # SaveImage – last frame
    saveVideoNode  = "344"   # SaveVideo – full video

    if not os.path.exists(image_path):
        return {'error': f'Image not found: {image_path}'}

    if workflow_path is None:
        workflow_path = WORKFLOWS.get("i2v")

    if not os.path.exists(workflow_path):
        return {'error': f'Workflow file not found: {workflow_path}'}

    with open(workflow_path, "r") as f:
        workflow = json.load(f)

    # Input image placeholder
    workflow[imageNode]["inputs"]["image"] = "{{INPUT_IMAGE_PLACEHOLDER}}"

    # Prompt
    workflow[promptNode]["inputs"]["text"] = prompt
    workflow[negativeNode]["inputs"]["text"] = negative_prompt

    # Random seed
    seed = random.randint(1, 999_999_999_999_999)
    workflow[seedNode]["inputs"]["noise_seed"] = seed

    # Dimensions and frame count
    #workflow[widthNode]["inputs"]["value"] = width
    #workflow[heightNode]["inputs"]["value"] = height
    workflow[numFramesNode]["inputs"]["value"] = num_frames

    # Generate unique output filenames
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_filename = f"i2v_video_{timestamp}.mp4"
    image_filename = f"i2v_lastframe_{timestamp}.png"

    os.makedirs(save_directory, exist_ok=True)

    _owns_client = comfy_client is None
    if _owns_client:
        print(f"Connecting to ComfyUI at {comfyui_ip}:{port}...")
        comfy_client = ComfyUIlocal(
            comfyui_ip=comfyui_ip,
            port=port,
            output_dir=save_directory,
            workflow_type='i2v'
        )

    try:
        result = comfy_client.generate(
            workflow=workflow,
            service_type='i2v',
            input_files={"{{INPUT_IMAGE_PLACEHOLDER}}": image_path},
            file_prefix={
                saveVideoNode: video_filename,
                saveImageNode: image_filename,
            },
            output_paths={
                saveVideoNode: os.path.join(save_directory, video_filename),
                saveImageNode: os.path.join(save_directory, image_filename),
            }
        )

        return {
            'success': True,
            'prompt': prompt,
            'seed': seed,
            'num_frames': num_frames,
            'output': result
        }

    except Exception as e:
        return {'error': str(e)}

    finally:
        if _owns_client:
            comfy_client.close()


def batch_generate_poses(
    image_path: str,
    category: str,
    pose_ids: list,
    save_directory: str,
    **kwargs
) -> list:
    """
    Generate multiple pose variations for the same input image.
    
    Args:
        image_path: Path to the input image
        category: Pose category
        pose_ids: List of pose IDs to generate (e.g., ['1', '2', '3'])
        save_directory: Directory to save output images
        **kwargs: Additional arguments passed to generate_pose
    
    Returns:
        List of generation results
    """
    results = []
    for pose_id in pose_ids:
        print(f"\nGenerating {category}/{pose_id}...")
        result = generate_pose(
            image_path=image_path,
            category=category,
            pose_id=pose_id,
            save_directory=save_directory,
            **kwargs
        )
        results.append(result)
        
        if 'error' in result:
            print(f"  Error: {result['error']}")
        else:
            print(f"  Success!")
    
    return results

def i2v_prompts():
    #flow concept:
    #1. use last frame to prompt to bj image
    #use bj frame to doggystyle
    enhancer_prompt = "text, watermark, realistic photo, 3d render, gradient background, cluttered, busy, low quality, blurry, sketch, hand drawn, cartoon, animation"
    negative_prompt = "distorted face, asymmetric eyes, strange mouth, disfigured, extra limbs, missing fingers, deformed hands, extra fingers, blurry, low resolution, shaky, pixelated, compression artifacts, flickering, frame drops, warped anatomy, unnatural skin, waxy skin, plastic skin, oversaturated, harsh lighting, cartoon, anime, illustration, painting, sketch, watermark, text, logo, morphing, glitch, jitter, artifacts, overexposure, subtitle, captions"
    x ={
        'nude': {
            'prompt': 'a beautiful woman, camera is focused on her. she says in a New York accent "Only this one time, OK?", she laughs flirtatiously. She reaches down, grabs the hem of her shirt, and slowly lifts it over her head, sliding her arms out of the sleeves one at a time, letting the fabric drape naturally as it falls away. The camera holds, her naked breasts and nipples are now visible. She raises her hands to her breasts pushing them together and then fondling them. She steps forward toward the camera, close up on her face she says "Mmmm... don\'t tell anyone about this. Ok?", she smiles and laughs flirtatiously.',
            'negative_prompt': negative_prompt,
            'enhancer_prompt': enhancer_prompt,
            'num_frames': 360,
        },
        'missionary': {
            'prompt': "a beautiful woman lying on her back, a man inserting his penis in her vagina, her facial expression changes to pleasure. his hips thrusting rhythmically, her pelvis rising to meet each thrust, her breasts bouncing and swaying with every hip impact. her shoulders lift slightly with each thrust. she lifts her chin as the penis inserts all the way in. his hips continue thrusting in a steady rhythm, her breasts jiggle with each impact, her pelvis arching upward meeting his thrusts. The camera pushes in slowly, she raises her legs up in the air, her hips continuing to thrust. she says \"Oh my god you're so deep... yes... oh... uh\"",
            'negative_prompt': negative_prompt,
            'enhancer_prompt': enhancer_prompt,
            'num_frames': 360,
        },
        'cowgirl': {
            'prompt': "a beautiful woman straddling a man, his penis going in and out of her vagina, she bouncing up and down on his penis in a cowgirl position. her hips moving in circular motions, her breasts bouncing and swaying with each bounce, her shoulders rolling forward and back. her pelvis grinding against his, her hips thrusting downward. the camera pushes in slowly. she lifts her legs, knees bent, feet on his thighs, her hips continuing to bounce and grind, her breasts jiggling with each movement. She says \"oh your dick feels so big\"",
            'negative_prompt': negative_prompt,
            'enhancer_prompt': enhancer_prompt,
            'num_frames': 360,
        },
        'doggystyle': {
            'prompt': "a beautiful woman on all fours, a man behind her thrusting his penis in her vagina, her hips bouncing forward and back with each thrust, her breasts swaying side to side. her shoulders moving with the rhythm, her pelvis pushing back to meet his thrusts. her ass cheeks squeezing with each impact. she looks back over her shoulder, her hips continuing to bounce and thrust. the camera pushes in slowly, her body rocking forward and back, breasts jiggling, pelvis meeting every thrust. she says \"Oh you're making my pussy feel so good\"",
            'negative_prompt': negative_prompt,
            'enhancer_prompt': enhancer_prompt,
            'num_frames': 360,
        },
        'blowjob': {
            'prompt': "a beautiful woman kneeling in front of a man performing oral sex, blowjob, she uses her hands to masturbate the penis at the same time.  the penis thrusts in and out of her mouth. The camera stays focues on her face.  She stops, the penis comes out of her mouth and she says \"I want you to come in my mouth\". Then the penis goes back in her mouth and she continues the blowjob, her head moving forward and backward in rythem.  A hand from the side of the frame comes and rests on the back of her head as she continues the blowjob",
            'negative_prompt': negative_prompt,
            'enhancer_prompt': enhancer_prompt,
            'num_frames': 360,
        },
        
    }
    #First Frame Last Frame prompts
    #missionary to bj - works
    #doggy to bj - works well
    #doggy to bj works
    y ={
        'sex_to_blowjob':'a beautiful woman having sex. Camera stays fixed as she changes positions,  she gets up and respositions herself, she is now kneeling in front of a man performing oral sex, blowjob, she uses her hands to masturbate the penis at the same time, she looks at the camera and says in a new york accent "oh yes".  the penis thrusts in and out of her mouth. The camera stays focues on her face, she maintains eye contact with the camera.  Then the penis goes back in her mouth and she continues the blowjob, her head moving forward and backward in rythem.  A hand from the side of the frame comes and rests on the back of her head as she continues the blowjob. Scene Sound: wet sucking, moaning with the penis thrusts, blowjob, new york accent',
        'bj_to_doggystyle':'a beautiful woman giving a blowjob. she stops and moves back and turns around. Camera stays fixed as she changes positions,  she gets up and respositions herself her ass in front of the penis, it slowly enters her vagina doggystyle sex. she moves her hips forward and backward the penis goes in and out, she moans with pleasure in a new york accent \"oh...uh...oh\". doggystyle thrusts in and out. The camera stays focues on her ass. Scene Sound: wet suction, moaning with the penis thrusts, doggystyle, new york accent',
    }
    return x

def t2i_prompts():
    enhancer_prompt = "text, watermark, realistic photo, 3d render, gradient background, cluttered, busy, low quality, blurry, sketch, hand drawn, cartoon, animation, plastic skin"
    negative_prompt = "RAW photo, woman, detailed eye lashes, detailed natural skin and blemishes, unretouched realistic skin, skin texture style, detailed skin pore,detailed skin, realsitic skin. 35mm film grain, film grain texture, analog film photography, Kodak Portra 400 pushed film, ISO 3200"
    x = {
        'anna': {'prompt': "an attractive woman with a large natural chest, wavy short blond hair, bright blue eyes with black eyeliner and dark mascara, full lips, direct eye contact, wearing a rose-colored silk slip dress, background a luxury bed with white sheets, candle light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'betty': {'prompt': "an attractive woman with a petite chest, long straight black hair, deep brown eyes with winged eyeliner and soft smoky shadow, plush lips, confident direct eye contact, wearing a fitted emerald satin evening gown, background a luxury bed with white sheets, candle light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'cindy': {'prompt': "an attractive woman with a full chest, shoulder-length auburn curls, striking green eyes with dramatic mascara and subtle bronze shimmer, soft full lips, direct eye contact, wearing a yellow lace bodysuit with a sheer wrap, background a luxury bed with white sheets, candle light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt,'num_frames': 1},
        'daisy': {'prompt': "an attractive woman with a medium natural chest, platinum silver pixie-cut hair, icy gray eyes with smudged black liner and bold lashes, defined full lips, direct eye contact, wearing a gold off-shoulder silk robe, background a luxury bed with white sheets, candle light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'ella': {'prompt': "an attractive woman with a very full chest, long honey-brown waves, hazel eyes with glossy lids and dark mascara, lush full lips, direct eye contact, wearing a black corset dress with delicate straps, background a luxury bed with white sheets, candle light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt,'num_frames': 1},
        'fiona': {'prompt': "an attractive woman with a medium natural chest, layered copper-red hair grazing her shoulders, vivid amber eyes with smoky eyeliner and long lashes, soft full lips, direct eye contact, wearing a shimmering teal mini dress, background a crowded bar dance floor with warm neon lights, candle light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'gina': {'prompt': "an attractive woman with a full chest, sleek jet-black bob hair, dark espresso eyes with dramatic cat-eye liner and mascara, plush lips, direct eye contact, wearing a fitted ruby satin halter dress, background a lively bar dance floor with glowing amber lights, candle light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'holly': {'prompt': "an attractive woman with a petite chest, tousled platinum-blond waves, icy blue eyes with glitter shadow and bold mascara, defined full lips, direct eye contact, wearing a silver sequined cocktail dress, background an upscale bar dance floor with colorful moving lights. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'ivy': {'prompt': "an attractive woman with a large natural chest, long chestnut-brown curls, rich green eyes with dark liner and soft bronze shimmer, full lips, direct eye contact, wearing a black velvet off-shoulder dress, background a stylish bar dance floor with moody red lighting. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'jade': {'prompt': "an attractive woman with a medium full chest, short wavy dark-blond hair, bright hazel eyes with winged eyeliner and thick mascara, lush lips, direct eye contact, wearing a hooded sweatshirt, background a busy bar dance floor with sparkling overhead lights. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt,'num_frames': 1},
        'kate': {'prompt': "an attractive woman with a medium natural chest, long wavy red hair, vivid green eyes with subtle liner and lifted lashes, full lips, direct eye contact, wearing a white string bikini, background a tropical cabana with linen curtains and dappled sunlight. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'luna': {'prompt': "an attractive woman with a petite chest, long straight silver-white hair, luminous violet eyes with soft shimmer and delicate liner, parted lips with a faint smile, direct eye contact, wearing a forest-green elf costume with gold trim and pointed ears, background an enchanted forest with shafts of golden light filtering through ancient trees. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'maya': {'prompt': "an attractive woman with a full chest, short dark natural curls, rich brown eyes with clean liner and bold lashes, soft plush lips, direct eye contact, wearing a fitted white nurse uniform with a red cross detail, background a rooftop deck at dusk with city lights glowing below. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography",'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'nina': {'prompt': "an attractive woman with a medium chest, long dark braided hair adorned with gold coins, smoldering brown eyes with kohl liner, bold lips, direct eye contact, wearing a weathered pirate blouse open at the collar with a wide leather belt and knee-high boots, background a rooftop deck at sunset with ocean visible in the distance. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'olivia': {'prompt': "an attractive woman with a large natural chest, long chestnut-brown hair, bright blue eyes with natural lashes and soft liner, warm full lips, direct eye contact, wearing a chunky cream knit sweater, background a sunlit open field of tall golden grass with soft bokeh sky. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'petra': {'prompt': "an attractive woman with a medium chest, sleek auburn bob cut, cool gray eyes with precise winged liner and soft mascara, defined lips, direct eye contact, wearing a flowy silk blouse in dusty rose tucked into high-waisted trousers, background a rooftop deck at golden hour with potted plants and a city skyline. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'quinn': {'prompt': "an attractive woman with a petite chest, long loose honey-blond curls adorned with tiny wildflowers, wide hazel eyes with a natural dewy look and soft mascara, soft parted lips, direct eye contact, wearing a sheer gossamer nymph dress draped loosely over bare skin with leaf accents, background a mossy forest glade with sunbeams and mist. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': "", 'enhancer_prompt': "", 'num_frames': 1},
        'rose': {'prompt': "an attractive woman with a full chest, long wavy brunette hair, warm brown eyes with bronzed shimmer shadow and thick lashes, lush lips, direct eye contact, wearing a coral triangle bikini, background a luxury beach cabana with white canvas shade and turquoise water behind. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'sarah': {'prompt': "an attractive woman with a medium chest, straight honey-blond hair, clear blue eyes with minimal makeup and lifted lashes, natural soft lips, direct eye contact, wearing an oversized caramel-brown cable-knit sweater, background a wildflower field under an overcast sky with soft diffused light. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'tara': {'prompt': "an attractive woman with a large natural chest, long flowing black waves, dark espresso eyes with soft smoky shadow and bold lashes, full lips, direct eye contact, wearing a sheer floral chiffon blouse over a bandeau, background a dense green forest with dappled sunlight and fallen leaves. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
    }
    y = {
        'bella': {'prompt': "a teen woman with a medium chest, long copper-braided hair wrapped in a silk emerald headscarf with brass gears and tiny watch cogs woven in, warm amber eyes with kohl-rimmed liner and gold leaf shimmer, bold burnt-orange lips, direct eye contact, wearing a tailored Victorian steampunk corset in deep burgundy velvet with brass buckles and copper piping, a brass monocle chain draped across her chest, a long tweed skirt with clockwork embroidery, background a bustling steampunk workshop with exposed copper pipes, ticking clockwork mechanisms, and warm gaslight lanterns. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'carla': {'prompt': "a older woman with a large natural chest, long honey-blonde hair pinned up in an elaborate Victorian updo with pearl hairpins and a lace choker, clear sapphire eyes with delicate winged liner and rose-tinted lashes, soft mauve lips, direct eye contact, wearing a cream silk Victorian blouse with ruffled cuffs and a high collar, a deep navy velvet bustier with intricate gold embroidery, a long flowing skirt with a subtle train, background a grand Victorian parlor with ornate gilded mirrors, heavy damask wallpaper, and a crystal chandelier casting warm candlelight. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'danielle': {'prompt': "a beautiful woman with a full chest, long chestnut-brown hair cascading in thick waves adorned with a golden laurel wreath, striking emerald eyes with a smoky bronze shadow and bold lashes, full terracotta lips, direct eye contact, wearing a draped ivory silk toga with a gold-threaded border, a delicate gold torque necklace, bare shoulders with a faint golden armlet, background a sun-drenched Roman temple courtyard with weathered marble columns, ivy-covered stone walls, and a distant view of the Roman forum. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'emma': {'prompt': "a sexy woman with a petite chest, short cropped jet-black hair with a shaved undercut, piercing hazel eyes with heavy black liner and smudged kohl, dark plum lips with a slight bite mark, direct eye contact, wearing a fishnet top under a cropped denim vest covered in hand-painted patches and pins, a studded leather belt, ripped black skinny jeans with safety pin details, background a gritty back alley at night with wet cobblestones, graffiti-covered walls, a flickering streetlamp, and a distant club thumping bass. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles, a small tattoo of a rose on her collarbone. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'faith': {'prompt': "a young woman with a very full chest, long wild black curls with a few copper braids and tiny bone charms, deep brown eyes with kohl-rimmed liner and a faint gold dust shimmer, full warm-brown lips, direct eye contact, wearing a hand-woven bohemian peasant blouse in faded indigo with embroidered floral patterns, a short pleated skirt in earthy terracotta, a leather belt with a tarnished silver buckle, bare feet with anklets, background a smoky gypsy campfire at midnight with a worn tent, scattered tarot cards, and a crackling fire casting dancing shadows. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'grace': {'prompt': "a woman with a medium chest, a sharp platinum-blonde bob with a deep side part, icy gray eyes with sharp cat-eye liner and a smoky charcoal shadow, bold crimson lips, direct eye contact, wearing a tailored 1920s flapper dress in black silk with silver sequin art deco patterns, a long pearl necklace, fingerless lace gloves, background a smoky speakeasy with mahogany bar top, crystal decanters, a jazz band in the background, and warm amber light filtering through cigarette smoke. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'hannah': {'prompt': "a milf woman with a petite chest, long straight raven-black hair with a single red silk ribbon tied at the end, luminous dark brown eyes with a subtle red liner and long natural lashes, soft cherry-blossom pink lips, direct eye contact, wearing a fitted crimson silk kimono with a golden crane pattern, a wide obi belt in deep purple, a delicate gold hairpin, background a traditional Japanese geisha house at twilight with paper lanterns, a raked zen garden, and cherry blossom petals drifting in the evening breeze. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'isabella': {'prompt': "a female model with a full chest, shoulder-length wavy auburn hair with a few silver streaks, warm hazel eyes with a soft bronze shadow and thick natural lashes, full rose-colored lips, direct eye contact, wearing a weathered brown leather aviator jacket with brass zippers, a cream cable-knit sweater underneath, high-waisted corduroy trousers, scuffed leather boots, a pair of vintage aviator goggles resting on her forehead, background a dusty 1940s airfield at golden hour with a weathered biplane, oil-stained concrete, and a vast open sky with wispy clouds. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'jasmine': {'prompt': "a super model with a large natural chest, long wavy dark-blonde hair with a few loose tendrils framing her face, striking sea-green eyes with a smoky teal shadow and bold lashes, full coral lips, direct eye contact, wearing a tattered Victorian mourning dress in deep charcoal with lace trim, a black velvet choker with a silver locket, a long lace parasol, background a foggy Victorian cemetery at dawn with weathered tombstones, twisted iron gates, and a pale mist rolling over the grass. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
            'alice': {'prompt': "a woman with a full chest, choppy ash-blonde shag haircut with a deep side part, steel blue eyes with heavy black smudged liner and chipped red lipstick, a small silver stud on her lip, direct eye contact, wearing a ripped black leather moto jacket over a band tee with a faded skull print, ripped high-waisted denim shorts, fingerless leather gloves with metal studs, background a dimly lit back alley nightclub with peeling brick walls, flickering neon signs, and a bass speaker stack. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles, a small scar on her cheekbone. gritty film photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},

    }

    z = {
        'chloe': {'prompt': "a candid close-up half-body shot of a woman with a full chest, messy beach-wave auburn hair tucked behind one ear, warm hazel eyes looking away thoughtfully with a bare natural look and tinted lip balm, wearing an oversized oat-colored linen shirt unbuttoned at the top over a white cotton tank, a thin gold chain necklace with a tiny moon pendant, background a sun-bleached coastal boardwalk with weathered wooden railings and distant ocean haze. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'delia': {'prompt': "a candid close-up half-body shot of a woman with a petite chest, tight dark brown curls piled in a messy bun with a few loose tendrils, deep umber eyes with a smudged espresso shadow and glossed lips, caught mid-laugh with head tilted back, wearing a cropped mustard knit cardigan over a black ribbed camisole, layered brass necklaces with geometric pendants, background a bustling farmers market with hanging dried flower bundles and woven baskets. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'elara': {'prompt': "a candid close-up half-body shot of a woman with a large natural chest, long straight chestnut hair with a blunt fringe, cool gray-green eyes with a thin brown liner and sheer berry lip stain, looking down at her hands with a quiet expression, wearing a vintage floral wrap blouse in muted sage and blush, a delicate rose-gold bracelet with tiny charms, background a quiet botanical garden greenhouse with glass panes and trailing ferns. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'freya': {'prompt': "a candid close-up half-body shot of a woman with a very full chest, platinum-blonde hair in a loose French braid draped over one shoulder, striking sapphire eyes with a frosty lavender shadow and frosted pink lips, glancing sideways with a playful smirk, wearing a cropped denim jacket over a lavender silk slip top, chunky silver hoop earrings, a studded leather choker, background a vibrant street art alley with colorful murals and a graffiti-covered brick wall. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'irene': {'prompt': "a candid close-up half-body shot of a woman with a medium chest, shoulder-length wavy salt-and-pepper hair, warm amber eyes with a soft terracotta shadow and a muted rose lip, caught adjusting her scarf with a distant gaze, wearing a camel wool trench coat collar turned up, a long pearl strand necklace, a silk pocket square in burnt orange, background a rainy Parisian café terrace with wet cobblestones and an umbrella stand. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'juno': {'prompt': "a candid close-up half-body shot of a woman with a full chest, short cropped natural afro with baby hairs, rich mahogany eyes with a bold graphic blue liner and nude matte lips, looking over her shoulder with a confident raised brow, wearing an off-shoulder terracotta ribbed knit top, oversized gold disc earrings, a beaded waist chain, background a sun-drenched rooftop terrace with string lights and potted succulents. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'kira': {'prompt': "a candid close-up half-body shot of a woman with a petite chest, long honey-blonde hair in twin low braids, luminous sea-foam green eyes with a dewy highlight and clear lip gloss, caught mid-yawn with one hand near her mouth, wearing an oversized pastel pink hoodie with a subtle cat embroidery, a thin choker with a tiny star, background a cozy ramen shop interior with warm wood paneling and a steaming broth pot. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'lila': {'prompt': "a candid close-up half-body shot of a woman with a medium full chest, wavy dark chocolate hair with copper balayage ends, warm sienna eyes with a bronzed cut crease and a terracotta lip, looking down while flipping through a vinyl record, wearing a vintage band tee in faded crimson under an open flannel in forest green, a leather cord necklace with a turquoise stone, background a dimly lit record store with shelves of albums and warm tungsten bulbs. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'nora': {'prompt': "a candid close-up half-body shot of a woman with a large natural chest, sleek jet-black hair in a high slicked ponytail, cool steel-blue eyes with a sharp black wing and a deep plum lip, caught scrolling on her phone with a faint amused smile, wearing a structured black blazer over a white silk camisole, a geometric gold cuff bracelet, a single diamond stud earring, background a modern art gallery with white walls and an abstract sculpture in soft focus. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
        'opal': {'prompt': "a candid close-up half-body shot of a woman with a full chest, wild curly auburn-red hair escaping a silk scrunchie, vivid topaz eyes with a shimmery champagne shadow and a peach lip gloss, caught mid-sip from a paper coffee cup with steam rising, wearing a cozy cream cable-knit turtleneck, a long silver pendant necklace with an opal stone, a wool beret in charcoal gray, background a misty autumn park with fallen amber leaves and a wrought-iron bench. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. candid street photography", 'negative_prompt': negative_prompt, 'enhancer_prompt': enhancer_prompt, 'num_frames': 1},
    }

    return z


if __name__ == "__main__":
    TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_7")
    COMFYUI_IP = "127.0.0.1"
    COMFYUI_PORT = 8000
    PROMPT_DICT = "nsfw_variation_moon_light"

    def first_saved_image(result):
        """Return the first saved image path from a generation result dict."""
        for f in result.get('output', {}).get('files', []):
            if f.get('saved_path') and f.get('mime_type', '').startswith('image/'):
                return f['saved_path']
        return None

    def first_saved_video(result):
        """Return the first saved video path from a generation result dict."""
        for f in result.get('output', {}).get('files', []):
            if f.get('saved_path') and f.get('mime_type', '').startswith('video/'):
                return f['saved_path']
        return None

    os.makedirs(TEST_DIR, exist_ok=True)

    characters   = t2i_prompts()
    nsfw_poses   = pose_prompts().get(PROMPT_DICT, {})
    video_prompts = i2v_prompts()
    nude_pose_id = next(
        (pose_id for pose_id, pose_data in nsfw_poses.items() if pose_data.get("position") == "nude"),
        None,
    )

    if not nude_pose_id:
        raise ValueError(f"No nude pose configured in {PROMPT_DICT} pose prompts.")

    for char_name, char_prompt_data in characters.items():
        char_dir = os.path.join(TEST_DIR, char_name)
        os.makedirs(char_dir, exist_ok=True)
        print(f"\n{'='*60}\nCharacter: {char_name}\n{'='*60}")

        client = ComfyUIlocal(
            comfyui_ip=COMFYUI_IP,
            port=COMFYUI_PORT,
            output_dir=char_dir,
        )

        try:
            # ── Step 1: Text to Image ─────────────────────────────────────
            print(f"[{char_name}] Generating base t2i image...")
            t2i_result = generate_t2i(
                image_prompt=char_prompt_data['prompt'],
                save_directory=char_dir,
                negative_prompt=char_prompt_data['negative_prompt'],
                enhance_prompt=char_prompt_data['enhancer_prompt'],
                comfy_client=client,
            )
            if 'error' in t2i_result:
                print(f"[{char_name}] t2i error: {t2i_result['error']}")
                continue

            original_image = first_saved_image(t2i_result)
            if not original_image:
                print(f"[{char_name}] Could not find saved t2i image, skipping character.")
                continue
            print(f"[{char_name}] Base image: {original_image}")

            # ── Step 2: Generate all nsfw pose images first ────────────────
            posed_images = {}
            generated_videos = []
            #first generate the base nude pose, then use that as the input for the rest of the poses to ensure consistent character details across all poses and videos
            nude_image_result = generate_pose(
                image_path=original_image,
                category=PROMPT_DICT,
                pose_id=nude_pose_id,
                save_directory=char_dir,
                comfy_client=client,
            )
            if 'error' in nude_image_result:
                print(f"[{char_name}/nude] Pose error: {nude_image_result['error']}")
                continue

            #use the nude pose as the base image for the rest of the poses to ensure consistent character details across all poses and videos
            base_image = first_saved_image(nude_image_result)

            for pose_id, pose_data in nsfw_poses.items():
                position = pose_data.get('position', f"pose_{pose_id}")
                pose_dir = os.path.join(char_dir, position)
                os.makedirs(pose_dir, exist_ok=True)

                print(f"\n[{char_name}/{position}] Generating i2i pose image...")
                if pose_id == nude_pose_id:
                    # For the base nude pose, use the original t2i image as input
                     pose_result = generate_pose(
                        image_path=original_image,
                        category=PROMPT_DICT,
                        pose_id=pose_id,
                        save_directory=pose_dir,
                        comfy_client=client,
                    )
                else:
                    print(f"[{char_name}/{position}] Using nude pose as base image for this pose variation...")
                    pose_result = generate_pose(
                        image_path=base_image,
                        #image_path=original_image,
                        category=PROMPT_DICT,
                        pose_id=pose_id,
                        save_directory=pose_dir,
                        comfy_client=client,
                    )
                
                if 'error' in pose_result:
                    print(f"[{char_name}/{position}] Pose error: {pose_result['error']}")
                    continue

                posed_image = first_saved_image(pose_result)
                if not posed_image:
                    print(f"[{char_name}/{position}] Could not find saved pose image, skipping.")
                    continue

                posed_images[position] = {
                    'image_path': posed_image,
                    'pose_dir': pose_dir,
                }
                print(f"[{char_name}/{position}] Pose image: {posed_image}")

            # ── Step 3: Generate videos from the collected pose images ─────
            for position, posed_data in posed_images.items():
                pose_dir = posed_data['pose_dir']
                posed_image = posed_data['image_path']

                print("HARD CODE WARNING! toon prompt for 3d render")
                if position in video_prompts:
                    prompt_data = video_prompts[position]
                    # If generating a 'nude' video, start from the base image with clothing
                    if position == "nude":
                        i2v_image_path = original_image
                    else:
                        i2v_image_path = posed_image
                    print(f"[{char_name}/{position}] Generating i2v video...")
                    i2v_result = generate_i2v(
                        image_path=i2v_image_path,
                        save_directory=pose_dir,
                        prompt=f"{prompt_data['prompt']} {prompt_data['enhancer_prompt']}".strip(),
                        negative_prompt=prompt_data['negative_prompt'],
                        num_frames=prompt_data['num_frames'],
                        comfy_client=client,
                    )
                    if 'error' in i2v_result:
                        print(f"[{char_name}/{position}] i2v error: {i2v_result['error']}")
                    else:
                        saved_video = first_saved_video(i2v_result)
                        print(f"[{char_name}/{position}] Video saved: {saved_video}")
                        if saved_video:
                            generated_videos.append({
                                'file_path': saved_video,
                            })
                else:
                    print(f"[{char_name}/{position}] No i2v prompt for this position, skipping video.")

            # ── Step 4: Join all generated videos into a final movie ───────
            if generated_videos:
                output_name = f"trimmed_joined_({len(generated_videos)}).mp4"
                output_path = os.path.join(char_dir, output_name)
                output_path = get_unique_filename(output_path)

                print(f"\n[{char_name}] Joining {len(generated_videos)} generated videos...")
                join_result = trim_and_join_clips(
                    clips=generated_videos,
                    output_file_name=output_path,
                    concat_mode="copy",
                    crf=18,
                    also_output_video=True,
                    also_output_audio=True,
                    audio_format="flac",
                )
                print(f"[{char_name}] Final joined movie: {join_result['combined']}")
            else:
                print(f"[{char_name}] No generated videos to join.")

        finally:
            client.close()
