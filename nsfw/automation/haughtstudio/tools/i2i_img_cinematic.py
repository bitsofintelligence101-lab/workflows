import json
import os
import random
from datetime import datetime

def i2i(prompt, image_path_list, negative_prompt, enhance_prompt, i2i_workflow_path, save_directory, file_name=None,width=1280, height=720):
    """edit an image. image_path_list is a list since up to 3 media files can be provided."""
    print(f"\n\nReceived image edit request for prompt:\n '{prompt}'\n\n\n")

    if not prompt:
        return {'error': 'Prompt is required'}

    if not image_path_list:
        return {'error': 'imagePath is required'}
    
    if not isinstance(image_path_list, list):
        return {'error': 'imagePath must be a list of image file paths'}
    
    print(f"Using workflow: {i2i_workflow_path} with {len(image_path_list)} image(s)")
   
    
    with open(i2i_workflow_path, "r") as f:
        workflow = json.load(f)

    prompt_node = "876"
    negative_prompt_node = "872"
    enhance_prompt_node = "926"
    enhance_prompt_body_node = "975"
    pixar_lora_node = "945"
    qwen_nsfw_lora_node = "946"
    zit_nsfw_lora_node = "293"
    image1_input_node = "905"
    image2_input_node = "907"
    image3_input_node = "909"
    save_image_node = "886"
    noise_node = "880"

    # Always set image1; use it as a placeholder for unused load nodes
    image_1 = image_path_list[0]
    workflow[image1_input_node]["inputs"]["image"] = "{{INPUT_IMAGE1_PLACEHOLDER}}"
    input_files = {"{{INPUT_IMAGE1_PLACEHOLDER}}": image_1}

    if len(image_path_list) > 1:
        image_2 = image_path_list[1]
        workflow[image2_input_node]["inputs"]["image"] = "{{INPUT_IMAGE2_PLACEHOLDER}}"
        input_files["{{INPUT_IMAGE2_PLACEHOLDER}}"] = image_2
    else:
        # Point unused load node at image1 so the node stays valid
        workflow[image2_input_node]["inputs"]["image"] = "{{INPUT_IMAGE1_PLACEHOLDER}}"

    if len(image_path_list) > 2:
        image_3 = image_path_list[2]
        workflow[image3_input_node]["inputs"]["image"] = "{{INPUT_IMAGE3_PLACEHOLDER}}"
        input_files["{{INPUT_IMAGE3_PLACEHOLDER}}"] = image_3
    else:
        # Point unused load node at image1 so the node stays valid
        workflow[image3_input_node]["inputs"]["image"] = "{{INPUT_IMAGE1_PLACEHOLDER}}"

    # Remove unused image references from the prompt node inputs
    if len(image_path_list) < 3:
        workflow[prompt_node]["inputs"].pop("image3", None)
    if len(image_path_list) < 2:
        workflow[prompt_node]["inputs"].pop("image2", None)

    workflow[prompt_node]["inputs"]["prompt"] = prompt
    workflow[negative_prompt_node]["inputs"]["prompt"] = negative_prompt or "CGI, 3d render, cartoon, illustration, painting, drawing, lowres, bad anatomy, bad hands, distorted, oversaturated, watermark, signature, plastic look, plastic skin, low resolution, distortion, warped text, duplicate faces, out of focus, soft image, birthmark, acne"
    workflow[enhance_prompt_node]["inputs"]["text"] = enhance_prompt or "Shot on Kodak Portra 400 with warm color grading"
    #noise seed
    workflow[noise_node]["inputs"]["seed"] = random.randint(1, 1000000000)

    #Animation and 3D model LoRA boost
    
    if "pixar" in prompt.lower() or "3d animation" in prompt.lower():
        workflow[pixar_lora_node]["inputs"]["strength_model"] = 0.3
        #change negative prompt to avoid conflicting with Pixar style
        #workflow[negative_prompt_node]["inputs"]["prompt"] = negative_prompt or "realistic, illustration, painting, drawing, lowres, bad anatomy, bad hands, distorted, oversaturated, watermark, signature, low resolution, distortion, warped text, duplicate faces, out of focus, soft image, birthmark, acne"
        prompt = prompt.replace("pixar", "")
    

    if "nsfw" in prompt.lower():
        """
        LORA has key words, prompt should have one of these too for best results:
        bl0wj0b
        c0wg1rl
        r3v3rs3_c0wg1rl
        d0ubl3_j0b
        m15510n4ry
        d0gg13
        """
        workflow[qwen_nsfw_lora_node]["inputs"]["strength_model"] = 0.7 #qwen4play - too much and impacts face a lot
        workflow[zit_nsfw_lora_node]["inputs"]["strength_model"] = 1 #zit nsfw, add just a bit to help with genitals
        #focus the refiner prompt on the body for NSFW images to help with genitals.
        if "bl0wj0b" in prompt.lower():
            workflow[enhance_prompt_body_node]["inputs"]["text"] = "blowjob, detailed penis, Shot on a 50mm lens, Kodak Portra 400 with warm color grading"
        if "c0wg1rl" in prompt.lower():
            workflow[enhance_prompt_body_node]["inputs"]["text"] = "cowgirl, detailed vagina, penis, scrotum, Shot on a 50mm lens, Kodak Portra 400 with warm color grading"
        if "r3v3rs3_c0wg1rl" in prompt.lower():
            workflow[enhance_prompt_body_node]["inputs"]["text"] = "reverse cowgirl, detailed vagina, penis, scrotum,, Shot on a 50mm lens, Kodak Portra 400 with warm color grading"
        if "d0ubl3_j0b" in prompt.lower():
            workflow[enhance_prompt_body_node]["inputs"]["text"] = "blowjob, detailed penis, Shot on a 50mm lens, Kodak Portra 400 with warm color grading"
        if "m15510n4ry" in prompt.lower():
            workflow[enhance_prompt_body_node]["inputs"]["text"] = "missionary vaginal penetration, penis, detailed vagina, Shot on a 50mm lens, Kodak Portra 400 with warm color grading"
        if "d0gg13" in prompt.lower():
            workflow[enhance_prompt_body_node]["inputs"]["text"] = "doggy style, penis, anus, vagina, Shot on a 50mm lens, Kodak Portra 400 with warm color grading"
        #strip 'nsfw' from the prompt so it doesn't affect the model negatively
        prompt = prompt.replace("nsfw", "")
    

    #optional width and height parameters for the output image, default to 1280x720
    #workflow["241"]["inputs"]["width"] = width
    #workflow["241"]["inputs"]["height"] = height

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if file_name is None:
        output = f"image_edit_output_{timestamp}.png"
    else:
        if not isinstance(file_name, str) or not file_name.strip():
            print(f"Invalid file_name must be a string with no extension, using default output name.")
            output = f"image_edit_output_{timestamp}.png"
        else:
            output = f"{file_name}.png"

    comfy_request = {
        'workflow': workflow,
        'file_name': output,
        'input_files': input_files,
        'file_prefix': {save_image_node: output},
        'save_path': save_directory,
        'node_id': save_image_node,
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
    comfy_client = ComfyUIlocal()

    # CALL COMFYUI VRAM CLEAN HERE
    comfy_client.aggressive_cleanup()

    #Testing Files
    test_image_1 = r"C:\Users\jared\Documents\code\haughtstudio_AI\tools\test\single_woman_whitebackground.png"
    test_image_2 = r"C:\Users\jared\Documents\code\haughtstudio_AI\tools\test\single_man_whitebackground.png"
    test_image_3 = r"C:\Users\jared\Documents\code\haughtstudio_AI\tools\test\background_seaside_table.png"
    test_3xCharSheet = r"C:\Users\jared\Documents\code\haughtstudio_AI\tools\test\character_sheet_3image_00010_.png"

    #save output
    output_directory = "test/output"
    workflow_file = "comfy_workflows/qwen_image_edit_all_cinematic.json"

    ############################### 1 Image Input #####################################################

    print("\n\nTESTING 1 Image Input\n\n")
    test_prompt = "Show the subject in Image 1 on a beach, walking on the sidewalk of a busy new city street"
    test_negative_prompt = "blurry, low resolution, bad quality, cartoon, 3d render"
    test_enhance_prompt = "cinematic photograph, high contrast, professional photo, sharp focus"
    test_image_path_list = [test_image_1]

    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, output_directory)

    # Resolve workflow path relative to parent directory (project root)
    WORKFLOWS['i2i_cinematic'] = os.path.join(parent_dir, workflow_file)

    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)

    print(f"Testing i2i integration. Saving to {test_save_dir}")

    request_data = i2i(
        prompt=test_prompt,
        image_path_list=test_image_path_list,
        negative_prompt=test_negative_prompt,
        enhance_prompt=test_enhance_prompt,
        i2i_workflow_path=WORKFLOWS['i2i_cinematic'],
        save_directory=test_save_dir
    )

    if 'error' in request_data:
        print(f"Error building request: {request_data['error']}")
        sys.exit(1)


    print("Executing workflow...")
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type='i2i_cinematic',
        input_files=request_data['input_files'],
        file_prefix=request_data['file_prefix'],
        output_paths={request_data['node_id']: os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}
    )

    if result.get('files'):
        print(f"Success! Output saved to: {os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}")
    else:
        print("Failed to generate image:", result.get('error', 'Unknown error'))

    ############################### 2 Image Edit #####################################################

    print("\n\nTESTING 2 Image Input\n\n")
    test_prompt = "Show the subject in Image 1 standing behind a the whipstaff of a wooden ship, subject from image 2 standing next to them, open ocean background"
    test_negative_prompt = "blurry, low resolution, bad quality, cartoon, 3d render"
    test_enhance_prompt = "cinematic photograph, high contrast, professional photo, sharp focus"
    test_image_path_list = [test_image_1, test_image_2]

    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, output_directory)

    # Resolve workflow path relative to parent directory (project root)
    WORKFLOWS['i2i_cinematic'] = os.path.join(parent_dir, workflow_file)

    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)

    print(f"Testing i2i integration. Saving to {test_save_dir}")

    request_data = i2i(
        prompt=test_prompt,
        image_path_list=test_image_path_list,
        negative_prompt=test_negative_prompt,
        enhance_prompt=test_enhance_prompt,
        i2i_workflow_path=WORKFLOWS['i2i_cinematic'],
        save_directory=test_save_dir
    )

    if 'error' in request_data:
        print(f"Error building request: {request_data['error']}")
        sys.exit(1)


    print("Executing workflow...")
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type='i2i_cinematic',
        input_files=request_data['input_files'],
        file_prefix=request_data['file_prefix'],
        output_paths={request_data['node_id']: os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}
    )

    if result.get('files'):
        print(f"Success! Output saved to: {os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}")
    else:
        print("Failed to generate image:", result.get('error', 'Unknown error'))

    ################################# 3 Image Edit #############################################################

    print("\n\nTESTING 3 Image Input\n\n")
    test_prompt = "Show the subject in Image 1 and the subject from image 2 sitting together at the table shown in Image 3"
    test_negative_prompt = "blurry, low resolution, bad quality, cartoon, 3d render"
    test_enhance_prompt = "cinematic photograph, high contrast, professional photo, sharp focus"
    test_image_path_list = [test_image_1, test_image_2, test_image_3]

    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, output_directory)

    # Resolve workflow path relative to parent directory (project root)
    WORKFLOWS['i2i_cinematic'] = os.path.join(parent_dir, workflow_file)

    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)

    print(f"Testing i2i integration. Saving to {test_save_dir}")

    request_data = i2i(
        prompt=test_prompt,
        image_path_list=test_image_path_list,
        negative_prompt=test_negative_prompt,
        enhance_prompt=test_enhance_prompt,
        i2i_workflow_path=WORKFLOWS['i2i_cinematic'],
        save_directory=test_save_dir
    )

    if 'error' in request_data:
        print(f"Error building request: {request_data['error']}")
        sys.exit(1)


    print("Executing workflow...")
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type='i2i_cinematic',
        input_files=request_data['input_files'],
        file_prefix=request_data['file_prefix'],
        output_paths={request_data['node_id']: os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}
    )

    if result.get('files'):
        print(f"Success! Output saved to: {os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}")
    else:
        print("Failed to generate image:", result.get('error', 'Unknown error'))


    ################################# Char Sheet Edit #############################################################

    print("\n\nTESTING character sheet Input\n\n")
    test_prompt = "use the reference sheet of subject in Image 1. Show subject sitting in a tavern at the bar with a beer in hand."
    test_negative_prompt = "blurry, low resolution, bad quality, cartoon, 3d render"
    test_enhance_prompt = "cinematic photograph, high contrast, professional photo, sharp focus"
    test_image_path_list = [test_3xCharSheet]

    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, output_directory)

    # Resolve workflow path relative to parent directory (project root)
    WORKFLOWS['i2i_cinematic'] = os.path.join(parent_dir, workflow_file)

    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)

    print(f"Testing i2i integration. Saving to {test_save_dir}")

    request_data = i2i(
        prompt=test_prompt,
        image_path_list=test_image_path_list,
        negative_prompt=test_negative_prompt,
        enhance_prompt=test_enhance_prompt,
        i2i_workflow_path=WORKFLOWS['i2i_cinematic'],
        save_directory=test_save_dir,
        file_name="test_i2i_charSheet_cinematic"
    )

    if 'error' in request_data:
        print(f"Error building request: {request_data['error']}")
        sys.exit(1)


    print("Executing workflow...")
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type='i2i_cinematic',
        input_files=request_data['input_files'],
        file_prefix=request_data['file_prefix'],
        output_paths={request_data['node_id']: os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}
    )

    if result.get('files'):
        print(f"Success! Output saved to: {os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}")
    else:
        print("Failed to generate image:", result.get('error', 'Unknown error'))

    ################################# NSFW #############################################################

    print("\n\nTESTING character sheet Input\n\n")
    test_prompt = "subject in Image 1. Show subject sitting in a tavern at the bar with a beer in hand. they are naked, breasts and nipples exposed, navel and hips, vagina slightly visible between legs. nsfw"
    test_negative_prompt = "blurry, low resolution, bad quality, cartoon, 3d render"
    test_enhance_prompt = "cinematic photograph, high contrast, professional photo, sharp focus"
    test_image_path_list = [test_image_1]

    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_save_dir = os.path.join(current_dir, output_directory)

    # Resolve workflow path relative to parent directory (project root)
    WORKFLOWS['i2i_cinematic'] = os.path.join(parent_dir, workflow_file)

    if not os.path.exists(test_save_dir):
        os.makedirs(test_save_dir)

    print(f"Testing i2i integration. Saving to {test_save_dir}")

    request_data = i2i(
        prompt=test_prompt,
        image_path_list=test_image_path_list,
        negative_prompt=test_negative_prompt,
        enhance_prompt=test_enhance_prompt,
        i2i_workflow_path=WORKFLOWS['i2i_cinematic'],
        save_directory=test_save_dir,
        file_name="test_i2i_nsfw_cinematic"
    )

    if 'error' in request_data:
        print(f"Error building request: {request_data['error']}")
        sys.exit(1)


    print("Executing workflow...")
    result = comfy_client.generate(
        workflow=request_data['workflow'],
        service_type='i2i_cinematic',
        input_files=request_data['input_files'],
        file_prefix=request_data['file_prefix'],
        output_paths={request_data['node_id']: os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}
    )

    if result.get('files'):
        print(f"Success! Output saved to: {os.path.join(request_data['save_path'], request_data['file_name'] + '.png')}")
    else:
        print("Failed to generate image:", result.get('error', 'Unknown error'))