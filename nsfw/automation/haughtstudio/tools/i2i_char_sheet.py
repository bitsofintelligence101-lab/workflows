import json
import os
import random
from datetime import datetime

# ---------------------------------------------------------------------------
# Node map for comfy_workflows/character_sheet_H3_cinematic.json
#
# The workflow renders 5 views of a character (Qwen Image Edit 2511 +
# multiple-angles LoRA), refines each render with a ZIT FaceDetailer pass,
# then stitches the results into 3-up and 5-up character sheets.
# It returns MANY images: 5 refined views + the base reference + 2 sheets.
# ---------------------------------------------------------------------------

# Prompt nodes (PrimitiveStringMultiline) - one prompt per character-sheet view
VIEW_PROMPT_NODES = {
    "face_front_close": "509",    # Face Front Close
    "quarter_turn_front": "536",  # Half Body Quarter Turn Front (right side view)
    "face_profile_close": "523",  # Face Profile Close (left side view)
    "full_body_front": "549",     # Full Body Front
    "half_body_back": "900",      # Half Body Back
    "clean_background": "412",     # Clean Background (base reference)
}

# TextEncodeQwenImageEditPlus nodes holding the hardcoded negative prompt
NEGATIVE_PROMPT_NODES = ["497", "512", "525", "538", "916"]

REFINER_PROMPT_NODE = "749"  # "ZIT Refiner Prompt" shared by every FaceDetailer pass
SEED_NODE = "505"            # PrimitiveInt feeding all 5 KSamplers

# LoadImage nodes. 41 is the actual character reference
MAIN_IMAGE_NODE = "41"

# LoRA strength nodes
QWEN_NSFW_LORA_NODE = "1055"  # Qwen4Play2512_v2 (Qwen edit model)
ZIT_NSFW_LORA_NODE = "575"    # NSFW_master_ZIT (face refiner model)
PIXAR_LORA_NODE = "1054"  # Pixar_LoRA (boost for pixar/3d animation style)

# SaveImage node -> output filename suffix (this workflow returns many images)
SAVE_IMAGE_NODES = {
    "654": "p1_front_close",    # refined render of prompt node 509
    "657": "p2_profile_close",  # refined render of prompt node 523
    "656": "p3_full_body",      # refined render of prompt node 549
    "655": "p4_quarter_turn",   # refined render of prompt node 536
    "927": "p5_back",           # refined render of prompt node 900
    "751": "cleanbackground",   # base reference, re-saved
    "1047": "charSheet_x3",     # stitched 3-up face sheet
    "1070": "main_char_sheet",     # stitched 3-up mid range (primary output)
    "1018": "charSheet_x4",     # stitched 4-up sheet 
}
PRIMARY_SAVE_NODE = "1070"

DEFAULT_NEGATIVE_PROMPT = (
    "CGI, 3d render, cartoon, illustration, painting, drawing, lowres, "
    "bad anatomy, bad hands, distorted, oversaturated, watermark, signature, "
    "plastic look, plastic skin, low resolution, distortion, warped text, "
    "duplicate faces, out of focus, soft image, birthmark, acne"
)
DEFAULT_REFINER_PROMPT = "Shot on a 50mm lens, Kodak Portra 400 with warm color grading"


def i2i(prompt, image_path_list, negative_prompt, enhance_prompt, i2i_workflow_path, save_directory, body_type=None, breasts=None, face_accessories=None, upper_clothing=None, lower_clothing=None, file_name="primary_char_sheet", background="all white background"):
    """Build a character-sheet ComfyUI request.

    `prompt` may be either:
      nsfw, or pixar or both. it's a flag not a true prompt. this worflow has existing defined prompts for each view.

    `image_path_list` is a list for interface compatibility; this workflow only
    uses the first image as the character reference.

    Optional detail args are appended verbatim (one per line) to the end of
    specific view prompts, on top of whatever prompt the view already has:
      - face_accessories, upper_clothing -> face_front_close (509)
      - face_accessories, upper_clothing, lower_clothing -> full_body_front (549)
    """
    print(f"\n\nReceived character sheet request for prompt:\n '{prompt}'\n\n\n")

    if not prompt:
        return {'error': 'Prompt is required'}

    if not image_path_list:
        return {'error': 'imagePath is required'}

    if not isinstance(image_path_list, list):
        return {'error': 'imagePath must be a list of image file paths'}

    print(f"Using workflow: {i2i_workflow_path} with reference image: {image_path_list[0]}")
    if len(image_path_list) > 1:
        print("Note: character sheet workflow uses a single reference image; extra images ignored.")

    with open(i2i_workflow_path, "r") as f:
        workflow = json.load(f)

    # Set the primary char sheet file name
    SAVE_IMAGE_NODES[PRIMARY_SAVE_NODE] = file_name

    #prompt face front close
    workflow[VIEW_PROMPT_NODES["face_front_close"]]["inputs"]["value"] = f"<sks> front view eye-level shot medium shot\
subject is {upper_clothing}, {face_accessories}, {body_type} body, {breasts} breasts\
exact same {background}"

    #prompt full body front
    workflow[VIEW_PROMPT_NODES["full_body_front"]]["inputs"]["value"] = f"<sks> full body front view eye-level shot medium shot\
subject is {upper_clothing}, {face_accessories}, {lower_clothing}, {body_type} body, {breasts} breasts\
exact same {background}"

    #update backgrounds
    workflow[VIEW_PROMPT_NODES["clean_background"]]["inputs"]["prompt"] = f"Keep the person's face, identity, pose, exactly the same. Remove clothing strap from subject's shoulder\nmake the image have an {background}"
    workflow[VIEW_PROMPT_NODES["quarter_turn_front"]]["inputs"]["value"] = f"<sks> right side view eye-level shot medium shot\nexact same {background}"
    workflow[VIEW_PROMPT_NODES["face_profile_close"]]["inputs"]["value"] = f"<sks> left side view eye-level shot close-up\nexact same {background}"
    workflow[VIEW_PROMPT_NODES["half_body_back"]]["inputs"]["value"] = f"<sks> back view eye-level shot medium shot\nexact same {background}"

    
    
    # --- Negative prompts (same text in all 5 encode nodes) -----------------
    for node_id in NEGATIVE_PROMPT_NODES:
        workflow[node_id]["inputs"]["prompt"] = negative_prompt or DEFAULT_NEGATIVE_PROMPT

    # --- ZIT refiner (FaceDetailer) prompt ----------------------------------
    workflow[REFINER_PROMPT_NODE]["inputs"]["value"] = enhance_prompt or DEFAULT_REFINER_PROMPT

    # --- Seed (shared by every KSampler) ------------------------------------
    workflow[SEED_NODE]["inputs"]["value"] = random.randint(1, 1000000000)


    # --- Input image --------------------------------------------------------
    input_placeholder = "{{INPUT_IMAGE1_PLACEHOLDER}}"
    workflow[MAIN_IMAGE_NODE]["inputs"]["image"] = input_placeholder
    input_files = {input_placeholder: image_path_list[0]}

    # ---- Pixar LoRA boost ---------------------------------------------------
    if "pixar" in prompt.lower() or "3d animation" in prompt.lower():
            workflow[PIXAR_LORA_NODE]["inputs"]["strength_model"] = 0.3

    # --- NSFW LoRA boosts ---------------------------------------------------
    nsfw = False
    if "nsfw" in prompt.lower():
        nsfw = True
        """
        LORA has key words, prompt should have one of these too for best results:
        bl0wj0b
        c0wg1rl
        r3v3rs3_c0wg1rl
        d0ubl3_j0b
        m15510n4ry
        d0gg13
        """
        
        
        workflow[QWEN_NSFW_LORA_NODE]["inputs"]["strength_model"] = 0.7  # qwen4play - too much and impacts face a lot
        workflow[ZIT_NSFW_LORA_NODE]["inputs"]["strength_model"] = 0.5   # zit nsfw - need this low to retain skin detail, add just a bit to help with genitals
        # strip 'nsfw' from the prompts so it doesn't affect the model negatively
        for node_id in VIEW_PROMPT_NODES.values():
            value = workflow[node_id]["inputs"].get("value")
            if isinstance(value, str):
                workflow[node_id]["inputs"]["value"] = value.replace("nsfw", "")
    else:
        #shut off NSFW LoRA boosts if not nsfw
        workflow[QWEN_NSFW_LORA_NODE]["inputs"]["strength_model"] = 0.0  # qwen4play - too much and impacts face a lot
        workflow[ZIT_NSFW_LORA_NODE]["inputs"]["strength_model"] = 0.0  # zit nsfw - need this low to retain skin detail, add just a bit to help with genitals

    

    # --- PreviewImage nodes only produce duplicate temp downloads; rewire each
    # SaveImage straight to its own stitch node and drop the previews --------
    if "997" in workflow:
        workflow["1018"]["inputs"]["images"] = ["997", 0]          # charSheet_x4
    if "1068" in workflow:
        workflow[PRIMARY_SAVE_NODE]["inputs"]["images"] = ["1068", 0]  # main_char_sheet
    workflow.pop("1059", None)
    workflow.pop("1042", None)
    workflow.pop("1066", None)

    # --- Output filenames (one per SaveImage node) --------------------------
    # Safe auto-index: if any output file already exists in save_directory,
    # append _1, _2, ... to every suffix so runs never overwrite each other.
    def _unique_suffixes():
        index = 0
        while True:
            suffixes = {
                node_id: f"{suffix}.png" if index == 0 else f"{suffix}_{index}.png"
                for node_id, suffix in SAVE_IMAGE_NODES.items()
            }
            if not os.path.isdir(save_directory):
                return suffixes
            if not any(os.path.exists(os.path.join(save_directory, name))
                       for name in suffixes.values()):
                return suffixes
            index += 1

    file_prefix = _unique_suffixes()
    hero_output = file_prefix[PRIMARY_SAVE_NODE]

    comfy_request = {
        'workflow': workflow,
        'file_name': hero_output,
        'input_files': input_files,
        'file_prefix': file_prefix,
        'save_path': save_directory,
        'node_id': PRIMARY_SAVE_NODE,
        'image_node_id': MAIN_IMAGE_NODE,
        'output_node_ids': list(SAVE_IMAGE_NODES.keys()),
        'prompt_final': prompt if isinstance(prompt, str) else json.dumps(prompt)
    }

    return comfy_request


if __name__ == "__main__":
    import sys
    # ------------------------------------------------------------------
    # Local smoke test: builds the comfy_request and validates it against
    # the real workflow JSON. Does NOT contact a ComfyUI server.
    # ------------------------------------------------------------------
    # Add parent directory to path to import call_comfyui
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.append(parent_dir)
    from call_comfyui import ComfyUIlocal, WORKFLOWS
    
    # Initialize ComfyUI client
    comfy_client = ComfyUIlocal(last_workflow="i2i")
    
    # CALL COMFYUI VRAM CLEAN HERE
    comfy_client.aggressive_cleanup()
    
    PLACEHOLDER_INPUT_IMAGE = r"C:\Users\jared\Documents\code\haughtstudio\automation\test_10\nsfw_variation_5_close_20260806_210736.png"  # input image
    SAVE_DIR = r"C:\Users\jared\Documents\code\haughtstudio\automation\test_10"  # Test output folder
    WORKFLOW_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "comfy_workflows",
        "character_sheet_H3_cinematic.json",
    )


    print(f"Workflow: {WORKFLOW_PATH}\n")

    
    # --- Test: nsfw keyword triggers LoRA boosts and gets stripped ------
    req3 = i2i(
        prompt="nsfw ",
        image_path_list=[PLACEHOLDER_INPUT_IMAGE],
        negative_prompt=None,
        enhance_prompt=None,
        i2i_workflow_path=WORKFLOW_PATH,
        save_directory=SAVE_DIR,
        face_accessories="gold necklace",
        upper_clothing="hot red lace bra",
        lower_clothing="red thong and knee-high stockings",
        file_name="test_nsfw",
    )

    print("Executing workflow...")
    # Save every output node: output_paths only needs the directory — generate()
    # appends each node's file_prefix name automatically.
    output_paths = {node_id: req3['save_path'] for node_id in req3['file_prefix']}
    result = comfy_client.generate(
        workflow=req3['workflow'],
        service_type='i2i',
        input_files=req3['input_files'],
        file_prefix=req3['file_prefix'],
        output_paths=output_paths
    )
    
    if result.get('files'):
        print(f"Success! Output saved to: {os.path.join(req3['save_path'], req3['file_name'] + '.png')}")
    else:
        print("Failed to generate image:", result.get('error', 'Unknown error'))

    

   