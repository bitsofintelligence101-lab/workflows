import subprocess
import sys
import time
from urllib import response
import requests
import os
import json
import psutil
import random
import torch
from flask import Flask


SERVICES_REGISTRY = {} # Maps service_type (tts, t2v, etc.) -> {url, port, config} for the running service instance(s)
#For COMFYUI, hardcode the worflows for different endpoints for now.
WORKFLOWS = {'t2i': "services/workflows/f4f_delete.json",
    #'t2i': "services/workflows/sdxl_lustify_endgame_enhanced.json",
    #'t2i': "services/workflows/sdxl_lustify_T2I_hs.json",
             "i2i": "services/workflows/qwen_image_edit_XLfast.json",
             "i2v": "services/workflows/wan22_nsfw_XLdelete.json",
             "v2v": "services/workflows/SVI_delete_enhanced.json",
             "video_lipsync": "services/workflows/lipsync_delete.json"}
PROJECTS_ROOT = os.path.join(os.getcwd(), 'projects')

WAN_LORA_FILES_MAP= {'blowjob': {'high': "Blowjob\\wan2.2-i2v-high-oral-insertion-v1.0.safetensors", 
                                'low': "Blowjob\\wan2.2-i2v-low-oral-insertion-v1.0.safetensors"},

                     'doggystyle': {'high': "DoggyStyle\\Wan2.2-Doggy_high_noise.safetensors" , 
                               'low': "DoggyStyle\\Wan2.2_I2V_Doggy_Style_14B_low_noise.safetensors"},

                     'missionary': {'high': "Missionary\\wan2.2_i2v_highnoise_pov_missionary_v1.0.safetensors", 
                                    'low': "Missionary\\wan2.2_i2v_lownoise_pov_missionary_v1.0.safetensors"},

                     'cowgirl': {'high': "CowGirl\\WAN-2.2-I2V-POV-Cowgirl-HIGH-v1.0-fixed.safetensors", 
                                 'low': "CowGirl\\WAN-2.2-I2V-POV-Cowgirl-LOW-v1.0-fixed.safetensors"},
                  
                      'fingering': {'high':"Fingering\\Sensual_fingering_v1_high_noise.safetensors", 
                                    'low': "Fingering\\Sensual_fingering_v1_low_noise.safetensors"},

                    'oral_insertion': {'high': "Blowjob\\wan2.2-i2v-high-oral-insertion-v1.0.safetensors", 
                                       'low': "Blowjob\\wan2.2-i2v-low-oral-insertion-v1.0.safetensors"},

                     'ejaculation': {'high': "Cumshot\\23High_noise-Cumshot_Aesthetics.safetensors", 
                                     'low': "Cumshot\\56Low_noise-Cumshot_Aesthetics.safetensors"},

                     'tit_fuck':{'high': "TitFuck\\WAN-2.2-I2V-POV-Titfuck-Paizuri-HIGH-v1.0.safetensors", 
                                 'low': "TitFuck\\WAN-2.2-I2V-POV-Titfuck-Paizuri-LOW-v1.0.safetensors"},

                    'rapid_action':{'high': None, 
                                 'low': None},

                    'basic':{'high': None, 'low': None}
                     }



# ========================================
# CONFIGURATION
# ========================================
app = Flask(__name__)

# ========================================
# CLEANUP UTILITIES
# ========================================

def kill_rogue_processes():
    """
    Kill any existing Python processes running model_server.py or qwen3VLM services.
    Does NOT kill ComfyUI processes.
    """
    print("Checking for rogue processes...")
    print("  Looking for model_server.py and qwen3VLM-related processes only.")
    current_pid = os.getpid()
    killed_count = 0
    
    # Keywords to target - more specific to avoid killing ComfyUI
    target_keywords = [
        'model_server.py',
    ]

    #Add any of the service module names
    for service in SERVICES_REGISTRY.get('module_name', []):
        target_keywords.append(service['module_name'].lower())
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            # Skip current process
            if proc.info['pid'] == current_pid:
                continue
                
            # Check if it's a Python process
            if proc.info['name'] and 'python' in proc.info['name'].lower():
                cmdline = proc.info['cmdline']
                if cmdline:
                    cmdline_str = ' '.join(cmdline).lower()
                    
                    # Check for targeted keywords
                    matched_keyword = None
                    for keyword in target_keywords:
                        if keyword in cmdline_str:
                            matched_keyword = keyword
                            break
                    
                    if matched_keyword:
                        # Show more of the command line for debugging
                        cmdline_preview = ' '.join(cmdline[:5]) if len(cmdline) > 5 else ' '.join(cmdline)
                        print(f"  Killing PID {proc.info['pid']} (matched '{matched_keyword}')")
                        print(f"    Command: {cmdline_preview}")
                        proc.kill()
                        proc.wait(timeout=5)
                        killed_count += 1
                        
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
            continue
    
    if killed_count > 0:
        print(f"  Killed {killed_count} rogue process(es)")
        time.sleep(2)  # Wait for processes to fully terminate
    else:
        print("  No rogue processes found")


def clear_gpu_memory():
    """
    Aggressively clear GPU VRAM and RAM.
    """
    print("Clearing GPU and system memory...")
    
    # Clear any existing PyTorch CUDA cache
    if torch.cuda.is_available():
        print(f"  GPU Count: {torch.cuda.device_count()}")
        
        # Clear cache for all available GPUs
        for i in range(torch.cuda.device_count()):
            if i == 0:
                print(f"GPU 0 is being used by ComfyUI, clearing cache, if this causes problems  skip GPU 0")
                #continue
            with torch.cuda.device(i):
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
                print(f" GPU {i}: {torch.cuda.get_device_name(i)} - cache cleared")
                
                # Print memory stats
                allocated = torch.cuda.memory_allocated(i) / 1024**3
                reserved = torch.cuda.memory_reserved(i) / 1024**3
                print(f"    GPU {i}: Allocated: {allocated:.2f} GB, Reserved: {reserved:.2f} GB")
    else:
        print("  No CUDA GPUs available")
    
    # Run Python garbage collection multiple times for thorough cleanup
    import gc
    for _ in range(3):
        gc.collect()
    
    print("  Memory cleared")


def full_cleanup():
    """
    Complete cleanup: kill rogue processes and clear all memory.
    Call this before starting model servers.
    """
    print("\n" + "="*50)
    print("STARTING FULL CLEANUP")
    print("="*50)
    
    #KILL ROGUGE PROCESS WILL KILL COMFYUI TOO!!
    kill_rogue_processes()
    clear_gpu_memory()
    
    print("="*50)
    print("CLEANUP COMPLETE")
    print("="*50 + "\n")

# ========================================
# GENERAL UTILITIES
# ========================================

def combine_videos(video_list=None, output_file_name=None, mode="auto", projects_folder=None, project_name=None):
    """
    Combines a list of MP4 video files into a single output file using FFmpeg.
    
    Args:
        video_list: A list of strings, where each string is the file path to an input video.
        output_file_name: The desired name for the combined output file (e.g., "final_video.mp4").
        projects_folder: Optional base folder for projects (e.g., "projects"). If provided, output will be saved under this folder with project_name.
        project_name: Optional project name to create a subfolder under projects_folder for the output file.
        mode: Encoding mode:
              "auto" (default) - Stream copy video, re-encode audio to unified format.
                     Best for same resolution/fps but different audio formats.
              "copy" - Stream copy both video and audio (fastest, but requires
                       identical video AND audio formats).
              "reencode" - Re-encode everything with H.264/AAC (slowest, but 
                           handles any input format differences).
    """
    import tempfile
    import shutil

    if not video_list:
        raise ValueError("video_list cannot be empty")
    
    # Construct proper save path if project details are provided
    if projects_folder and project_name:
        # If PROJECTS_ROOT is defined globally and projects_folder is relative, use it
        if 'PROJECTS_ROOT' in globals() and not os.path.isabs(projects_folder):
            output_dir = os.path.join(PROJECTS_ROOT, projects_folder, project_name)
        else:
            output_dir = os.path.join(projects_folder, project_name)
            
        os.makedirs(output_dir, exist_ok=True)
        output_file_name = os.path.join(output_dir, output_file_name)
    
    if len(video_list) == 1:
        # Just copy the single file
        shutil.copy2(video_list[0], output_file_name)
        output_file_path = os.path.abspath(output_file_name)
        print(f"Single video copied to {output_file_path}")
        return output_file_path
    
    # Create a temporary file listing all videos for FFmpeg concat
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
        for video_path in video_list:
            # Escape single quotes and backslashes for FFmpeg
            escaped_path = video_path.replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")
        concat_file = f.name

    try:
        if mode == "reencode":
            # Full re-encode - handles any format differences
            cmd = [
                'ffmpeg',
                '-y',  # Overwrite output file if exists
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-c:v', 'libx264',  # H.264 codec for wide compatibility
                '-preset', 'medium',  # Balance between speed and quality
                '-crf', '23',  # Quality (lower = better, 18-28 is typical range)
                '-pix_fmt', 'yuv420p',  # Required for web/mobile compatibility
                '-c:a', 'aac',  # AAC audio codec
                '-ar', '44100',  # Unified sample rate
                '-ac', '2',  # Stereo output
                '-b:a', '128k',  # Audio bitrate
                '-movflags', '+faststart',  # Move moov atom to start for web streaming
                output_file_name
            ]
            mode_desc = "full re-encoding (video + audio)"
        elif mode == "copy":
            # Pure stream copy - fastest, requires identical formats
            cmd = [
                'ffmpeg',
                '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-c', 'copy',
                '-movflags', '+faststart',
                output_file_name
            ]
            mode_desc = "stream copy (no re-encoding)"
        else:  # "auto" - default
            # Stream copy video, re-encode audio to handle different sample rates
            cmd = [
                'ffmpeg',
                '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-c:v', 'copy',  # Keep original video quality
                '-c:a', 'aac',  # Re-encode audio to unified format
                '-ar', '44100',  # Unified sample rate (44.1kHz standard)
                '-ac', '2',  # Stereo output
                '-b:a', '128k',  # Audio bitrate
                '-movflags', '+faststart',
                output_file_name
            ]
            mode_desc = "video copy + audio re-encode"
        
        print(f"Combining {len(video_list)} videos using {mode_desc}...")
        for i, video_path in enumerate(video_list, 1):
            print(f"  {i:3d}. {os.path.basename(video_path)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"FFmpeg error: {result.stderr}")
            raise RuntimeError(f"FFmpeg failed with return code {result.returncode}")
        
        output_file_path = os.path.abspath(output_file_name)
        print(f"Successfully combined {len(video_list)} videos into {output_file_path}")
        
    finally:
        # Clean up temporary concat file
        os.unlink(concat_file)
        return output_file_path

def unicode_replace(text):
    if text is None or not isinstance(text, str) or text.strip() == "":
        return text
    """Replace Unicode special characters with ASCII equivalents for better TTS compatibility."""
    unicode_replacements = {
        '\u2019': "'",      # RIGHT SINGLE QUOTATION MARK → apostrophe
        '\u2018': "'",      # LEFT SINGLE QUOTATION MARK → apostrophe
        '\u201C': '"',      # LEFT DOUBLE QUOTATION MARK → double quote
        '\u201D': '"',      # RIGHT DOUBLE QUOTATION MARK → double quote
        '\u2013': '-',      # EN DASH → hyphen
        '\u2014': '-',      # EM DASH → hyphen
        '\u2026': '...',    # ELLIPSIS → three dots
        '\u00E9': 'e',      # É → e
        '\u00E8': 'e',      # È → e
        '\u00EA': 'e',      # Ê → e
    }
    
    cleaned_text = text
    for unicode_char, ascii_char in unicode_replacements.items():
        cleaned_text = cleaned_text.replace(unicode_char, ascii_char)
    
    return cleaned_text

def clean_dialogue_for_tts(dialogue_dict):
    """Clean dialogue by converting Unicode special characters to ASCII equivalents for TTS compatibility.
    Replaces smart quotes, dashes, and other problematic Unicode characters.
    
    Args:
        dialogue_dict: Dictionary with scene numbers as keys and dialogue strings as values
        
    Returns:
        Dictionary with cleaned dialogue strings
    """
    # Character replacement mapping: Unicode → ASCII equivalent
    unicode_replacements = {
        '\u2019': "'",      # RIGHT SINGLE QUOTATION MARK → apostrophe
        '\u2018': "'",      # LEFT SINGLE QUOTATION MARK → apostrophe
        '\u201C': '"',      # LEFT DOUBLE QUOTATION MARK → double quote
        '\u201D': '"',      # RIGHT DOUBLE QUOTATION MARK → double quote
        '\u2013': '-',      # EN DASH → hyphen
        '\u2014': '-',      # EM DASH → hyphen
        '\u2026': '...',    # ELLIPSIS → three dots
        '\u00E9': 'e',      # É → e
        '\u00E8': 'e',      # È → e
        '\u00EA': 'e',      # Ê → e
    }
    
    cleaned_dialogue = {}
    for scene_num, dialogue_text in dialogue_dict.items():
        if dialogue_text:  # Only process non-empty dialogue
            cleaned_text = dialogue_text
            cleaned_dialogue[scene_num] = unicode_replace(cleaned_text)
        else:
            cleaned_dialogue[scene_num] = dialogue_text
    
    return cleaned_dialogue

# ========================================
# SERVICE MANAGER (SUBPROCESSES)
# ========================================

def graceful_shutdown():
    """
    Gracefully shutdown all services:
    1. Call /unload endpoint on each service to properly release GPU memory
    2. Terminate subprocesses
    3. Clear GPU memory
    """
    print("\n" + "="*50)
    print("STARTING GRACEFUL SHUTDOWN")
    print("="*50)
    
    # Step 1: Call /unload on each service
    print("\nStep 1: Unloading models from services...")
    for service_type, services in SERVICES_REGISTRY.items():
        for service in services:
            try:
                unload_url = f"{service['url']}/unload"
                print(f"  Calling {unload_url}...")
                response = requests.post(unload_url, timeout=30)
                if response.status_code == 200:
                    print(f"    ✓ {service['config']['name']} unloaded successfully")
                else:
                    print(f"    ⚠ {service['config']['name']} unload returned status {response.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"    ✗ Error unloading {service['config']['name']}: {e}")
            except Exception as e:
                print(f"    ✗ Unexpected error unloading {service['config']['name']}: {e}")
    
    # Wait a moment for unload operations to complete
    print("  Waiting for unload operations to complete...")
    time.sleep(3)
    
    # Step 2: Terminate subprocesses
    print("\nStep 2: Terminating subprocesses...")
    for service_type, services in SERVICES_REGISTRY.items():
        for service in services:
            try:
                print(f"  Terminating {service['config']['name']} (PID: {service['process'].pid})...")
                service['process'].terminate()
                service['process'].wait(timeout=5)
                print(f"    ✓ {service['config']['name']} terminated")
            except subprocess.TimeoutExpired:
                print(f"    ⚠ {service['config']['name']} didn't terminate, killing...")
                service['process'].kill()
                service['process'].wait()
            except Exception as e:
                print(f"    ✗ Error stopping {service['config']['name']}: {e}")
    
    # Step 3: Clear GPU memory
    print("\nStep 3: Clearing GPU memory...")
    clear_gpu_memory()
    
    print("\n" + "="*50)
    print("GRACEFUL SHUTDOWN COMPLETE")
    print("="*50 + "\n")

def start_model_servers(model_configs):
    """
    Launches sub-servers as subprocesses and registers them.
    Uses model_server.py to host each model class.
    """
    print(f"--- Launching {len(model_configs)} AI Services ---")
    
    for config in model_configs:
        try:
            # Construct the command to run model_server.py with the model module/class
            cmd = [
                sys.executable,
                "-u",  # Add this flag for unbuffered output 
                "model_server.py",
                "--module", config['module_name'],
                "--class-name", config['class_name'],
                "--host", config['host'],
                "--port", str(config['port']),
                "--gpu-index", str(config['gpu_index'])
            ]
            
            print(f"Starting {config['name']} ({', '.join(config['services'])}) on port {config['port']} with GPU {config['gpu_index']}...")
            print(f"  Command: {' '.join(cmd)}")
            
            # Launch process non-blocking
            proc = subprocess.Popen(
                cmd,
                cwd=os.getcwd()
            )
            
            # Register in memory
            for service in config.get('services', []):
                if service not in SERVICES_REGISTRY:
                    SERVICES_REGISTRY[service] = []
                
                SERVICES_REGISTRY[service].append({
                    'url': f"http://{config['host']}:{config['port']}",
                    'process': proc,
                    'config': config
                })
            
        except Exception as e:
            print(f"Failed to start {config['name']}: {e}")

def forward_to_service(service_type, endpoint, data):
    """
    Finds a running service of the requested type and forwards the request.
    """
    if service_type not in SERVICES_REGISTRY or not SERVICES_REGISTRY[service_type]:
        return False, {"error": f"No service available for {service_type}"}, 503

    # Simple Load Balancing: Just pick the first one for now
    service = SERVICES_REGISTRY[service_type][0]
    target_url = f"{service['url']}{endpoint}"
    #data['service_type'] = service_type  # Include service type in data for better logging and debugging, also improved vram management in model_server.py by clearing VRAM after each request based on this field.
    

    try:
        print(f"Forwarding to: {target_url}")
        print(f"Data: {json.dumps(data)[:50]}")  # Log first 100 chars of data for debugging
        response = requests.post(target_url, json=data, timeout=900)  # Increased timeout for long-running tasks
        
        # If the response contains binary content (an image/audio file directly)
        if 'application/json' not in response.headers.get('Content-Type', ''):
            return True, response.content, response.status_code
            
        # If the response is JSON (metadata or error)
        return True, response.json(), response.status_code
            
    except requests.exceptions.RequestException as e:
        return False, {"error": f"Service communication failed: {str(e)}"}, 500
    

# ========================================
# AUTOMATIC ENGINEERING
# ========================================
def make_image_prompt(idea=None, exact_prompt=None,save_path='outputs', project_id='default_project',prevent_model_unload=True):
    global SERVICES_REGISTRY

    """Use the vlm model to get an image prompt to be used for text to image generation
    optional: idea is a seed to get a type of image prompt for a character look or style
    """
    service_type = 'vlm'

    prompt = """Create an image prompt to be used with Stable Diffusion model that will generate a realistic sexy image of a Woman.
    It should be broken down into components subject, clothing, setting, style. Each component should have sufficient details. 
    The prompt should be detailed and specific to create a vivid and compelling image.  
    The prompt may include NSFW language, and should be descriptive enough to guide the image generation process effectively.

    These should be upper body or half body portrait style images focused on the subject upper body.
    
    example:
    {'subject': 'a young woman with,large natural chest shoulders back, wavy short blond hair, sexy brown eyes with black eyeliner dark mascara',
    'clothing': 'wearing a rose colored silk slip dress',
    'setting': 'ancient egypt temple ruins in the background',
    'style': 'sharp focus on her face direct eye contact, natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography, daylight, 8k resolution'}
    
    subject is the physical attributes face description eye color, makeup (mascara, eyeliner) hair color and style, body description (breast size, tall, short, thin, curvy, etc.), pose.
    clothing is the outfit, accessories, colors, materials, how it fits on the UPPER BODY ONLY
    setting is the background, environment, location, time of day, weather, etc.
    style is the artistic style, film type, lighting, color grading, resolution, skin detail trigger words.

    You are limited on prompt length so keep it concise and focused on important details and attributes that drive image generation. 
    Limit adjectives, use descriptions common in Stable Diffusion prompts known to be effective.

    Be precise with details and do not create rogue content that will confuse the model. The model already knows how clothing should fit, just specify type and colors
    NEVER use 'negative' prompts don't say 'no bra' because the word 'bra' will trigger to model to generate one. All words contribute towards the image and there are NO negative prompts, specify what you want.

    HALF BODY PORTRAIT which means description of the face and chest ONLY
    RETURN JSON with schema:
    {'subject': 'str', 'clothing': 'str', 'setting': 'str', 'style': 'str'}
    ONLY return the JSON structure with no additional text or explanation. The JSON should be properly formatted and parsable.
    """
    if idea is not None:
        prompt += f" The image should be based on this idea: {idea}"
    
    if exact_prompt is not None:
        #convert the prompt to the expected format.
        prompt = f"""Extract the structure of this prompt and convert to JSON.
        Example Input:"blond woman with bright blue eyes, long wavy hair flowing, natural makeup, toned upper body, standing confidently, slight smile, shoulders back, chest exposed, bare midriff, thin strapless bikini top, fabric clings to body, gold leaf trim, matching bikini bottom with high cut sides, bare legs, tropical beach at golden hour, soft sand, palm fronds swaying, ocean in background, cabana with white linen curtains behind her, warm haze, humid air, half-body portrait, cinematic lighting, shallow depth of field, 8k resolution, ultra detailed skin texture, natural skin tones, warm sunset glow, filmic color grading, professional photography Portrait",

        Example Response:
        {{
        "subject": "blond woman with bright blue eyes, long wavy hair flowing, natural makeup, toned upper body, standing confidently, slight smile, shoulders back, chest exposed",
        "clothing": "bare midriff, thin strapless bikini top, fabric clings to body, gold leaf trim, matching bikini bottom with high cut sides, bare legs",
        "setting": "tropical beach at golden hour, soft sand, palm fronds swaying, ocean in background, cabana with white linen curtains behind her, warm haze, humid air",
        "style": "half-body portrait, cinematic lighting, shallow depth of field, 8k resolution, ultra detailed skin texture, natural skin tones, warm sunset glow, filmic color grading, professional photography",
        }}

        Now convert this prompt to JSON:
        {exact_prompt}

        RETURN JSON with schema:
        {{'subject': 'str', 'clothing': 'str', 'setting': 'str', 'style': 'str'}}
        """

    # Forwards the prompt to the VLM service to get an image prompt back
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            #universally Required arguments: save_path, project_id, service_type
            {'prompt': prompt, 'images': [], 'service_type': service_type, 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload}  # No images for this prompt, just text
        )
    print(f"Response from VLM service: {response_data}")

    return response_data

def make_voice_prompt(character_description="", save_path='outputs', project_id='default_project',prevent_model_unload=True):
    """Use the vlm model to get a voice prompt to be used for TTS voice generation. The prompt should include specific details about the character's voice, such as pitch, tone, accent, and any unique vocal characteristics that would help create a distinct and fitting voice for the character based on their description.
    """
    example ="A young woman with a sexy sultry voice who speaks in a low rhaspy tone. flirtatious and playful when she speaks"
    service_type = 'vlm'
    prompt = f"""Create a detailed voice prompt to be used with a Text-to-Speech (TTS) model for generating a unique and fitting voice for a character based on the following description: {character_description}. The prompt should include specific details about the character's voice, such as pitch, tone, accent, and any unique vocal characteristics that would help create a distinct and fitting voice for the character. Be creative and ensure that the prompt provides enough information for the TTS model to generate a voice that matches the character's personality and background."""
    
    # Forwards the prompt to the VLM service to get a voice prompt back
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            #universally Required arguments: save_path, project_id, service_type
            {'prompt': prompt, 'images': [], 'service_type': service_type, 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload}  # No images for this prompt, just text
        )
    print(f"Response from VLM service: {response_data}")
    return response_data

def image_review(prompt_data=None, image_path=None, save_path='outputs', project_id='default_project',prevent_model_unload=True):
    """Use the vlm model to review a generated image and give feedback on how to improve it based on the original prompt and the components of the prompt (subject, clothing, setting, style). 
    This feedback can then be used to iteratively improve the image through multiple generations.
    """
    prompt = f""""You need to rank the image along criteria from 1 to 10.  The Image should show this: {prompt_data['combined']}.\n
    Criteria:
    - Subject Accuracy: How well does the image depict the described subject (face, body, pose)? (1-10)
    Is the subject {prompt_data['subject']} accurate in the image? Are the physical attributes, facial features, body description, and pose well represented?
    - Clothing Accuracy: How accurately does the clothing in the image match the description? (1-10)
    Is the clothing in the image consistent with the description of {prompt_data['clothing']}? Are the outfit, accessories, colors, materials, and how it fits on the body accurately depicted?
    - Setting Accuracy: How well does the background and environment match the described setting? (1-10)
    Does the image background and environment reflect the described setting of {prompt_data['setting']}? Are the location, time of day, weather, and overall atmosphere consistent with the description?
    - Style Accuracy: How well does the artistic style, lighting, and overall aesthetic match the description? (1-10)
    Is the artistic style of the image consistent with the description of {prompt_data['style']}? Does the lighting, color grading, resolution, and overall aesthetic match the described style?
    - Overall Quality: How visually appealing and well-composed is the image? (1-10)

    Return JSON schema:
    {{
        "subject_accuracy": str(1 to 10),
        "clothing_accuracy": str(1 to 10),
        "setting_accuracy": str(1 to 10),
        "style_accuracy": str(1 to 10),
        "overall_quality": str(1 to 10),
        "reasoning": str (a brief explanation of the scores, highlighting specific strengths and weaknesses in the image based on the criteria),
        "improved_prompt": str (a revised and improved prompt that addresses the weaknesses of the image and would likely result in a better image if used for another generation)
    }}
    """
    service_type = 'vlm'
     # Forwards the prompt to the VLM service to get an image prompt back
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            #universally Required arguments: save_path, project_id, service_type
            {'prompt': prompt, 'images': [image_path], 'service_type':service_type,'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload}  # No images for this prompt, just text
        )
    print(f"Response from VLM service: {response_data}")
    return response_data

def video_review(scene=None, scene_narrative=None, video_path=None, shot_num="1",num_shots_in_scene="3",save_path='outputs', project_id='default_project', prevent_model_unload=True):
    """
    scene is the entire scene dict
    scene narrative is just the single sentence overview for the scene
    
    """
    animation_prompt = scene[shot_num]['animation_prompt']
    
    system_prompt = f"""
You are a quality control reviewer for an AI-generated first-person POV XXX film.
You are reviewing shot {shot_num} in a movie scene consisting of {num_shots_in_scene} shots.

The ENTIRE scene narrative covered across all {num_shots_in_scene} shots is as follows, SCENE NARRATIVE: "{scene_narrative}"

You are reviewing clip {shot_num} generated with the ANIMATION PROMPT: "{animation_prompt}"

Score the clip on these criteria:

1. SUBJECT PRESENCE   (0-10): Is the character visible, framed correctly, and consistent with their animation prompt?
2. MOTION QUALITY     (0-10): Is motion naturalistic and matching the animation prompt?
3. POV INTEGRITY      (0-10): Does the shot maintain strict first-person perspective throughout?
4. PROMPT ALIGNMENT   (0-10): Does what happens in the clip match what was asked for in the animation prompt?
5. SCENE ALIGNMENT    (0-10): Does the clip effectively contribute to delivering the overall scene narrative and story beat?
6. CONTINUITY         (0-10): Does it flow in a coherent manner with no anatomical inconsistencies?
7. SEXUAL CONTENT ACCURACY (0-10): Are the sexual acts depicted accurately and explicitly as described in the animation prompt, with correct anatomy and no censorship? (may not apply for every clip, so use judgment on whether penis, vagina, breasts, fallatio, sex, intercourse etc. are accurately represented to include in overall score)
if a scene is supposed to have sex must appear that a penis is going in and out of a vagina. blowjobs should have a penis going in and out of a womans mouth with head bob motion. sex from be hind penis enters and exits ass. if she is supposed to undress can you see her breasts. This is the most critical catagory.

WEIGHTED OVERALL SCORE:
  Subject Presence : 20%
  Motion Quality   : 20%
  POV Integrity    : 15%
  Prompt Alignment : 15%
  Scene Alignment  : 5%
  Continuity       : 5%
  Sexual Content Accuracy : 40%

PASS THRESHOLD: overall_score >= 8.0

SCENE CHANGE CONDITION (only if score < 5.0):
  For a very low score, Assess whether the story Scene Narrative can still be delivered comparing what is shown and what the desired SCENE NARRATIVE is, if there is too much deviation → set "scene_change_required" True, indicating a fresh cut scene transition is required since scene narrative can't be achieved in a continuous flow from the animation prompt

Gather your thoughts about why you scored the video this way compared to the animation prompt and scene narrative and return your 'reasoning' along with the other info and scores
Output strictly this JSON:
{{
    "subject_presence": 0,
    "motion_quality": 0,
    "pov_integrity": 0,
    "prompt_alignment": 0,
    "scene_alignment": 0,
    "continuity": 0,
    "sexual_content_accuracy": 0,
    "reasoning":"basis and reasonining for the scores, what should be done to improve the scores via prompt changes",
    "scene_change_required": false
}}

"""
    prompt =f"""Please review the video clip for shot {shot_num} in the scene. The clip was generated with the animation prompt: "{animation_prompt}". The overall scene narrative is: "{scene_narrative}". Score the clip on the criteria provided and return the JSON with scores, reasoning, and whether a scene change is required if the score is very low. Be honest and critical in your review to ensure high quality output. Return JSON only"""
    
    # Forwards the prompt to the VLM service to get an image prompt back
    service_type = 'vlm'
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'prompt': prompt, 'system_prompt': system_prompt, 'images': [], 'videos': [video_path], 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload} 
        )
    print(f"Response from VLM service: {response_data}")
    return response_data

def video_review2(video_path=None, animation_prompt=None, anchor_image_prompt=None, save_path='outputs', project_id='default_project', prevent_model_unload=True):
    """
    scene is the entire scene dict
    scene narrative is just the single sentence overview for the scene
    
    """
    
    system_prompt = f"""
You are a quality control reviewer for an AI-generated first-person POV film.
You are reviewing a shot in a movie scene the was rendered with this prompt:
{animation_prompt}.

The shot was also influenced by an image, that showed: {anchor_image_prompt}

Review the video clip and score it on these criteria, which should be based on the
ANIMATION PROMPT: {animation_prompt}
and
ANCHOR PROMPT: {anchor_image_prompt}

Score the clip on these criteria:

1. SUBJECT PRESENCE   (0-10): Is the female woman face visible, framed correctly, and consistent with their animation prompt?
2. MOTION QUALITY     (0-10): Is motion naturalistic and matching the animation prompt?
3. POV INTEGRITY      (0-10): Does the shot maintain strict first-person perspective throughout?
4. PROMPT ALIGNMENT   (0-10): Does what happens in the clip match what was asked for in the animation prompt?
5. SCENE ALIGNMENT    (0-10): Does the clip effectively contribute to delivering the overall scene narrative of rhe anchor prompt?
6. CONTINUITY         (0-10): Does it flow in a coherent manner with no anatomical inconsistencies (no testicals on a woman, etc)?
7. SEXUAL CONTENT ACCURACY (0-10): If there are sexual acts, are the sexual acts depicted accurately and explicitly as described in the animation prompt and consistent with the Anchor prompt? (may not apply for every clip, so use judgment on whether penis, vagina, breasts, fallatio, sex, intercourse etc. are accurately represented to include in overall score)
if a scene is supposed to have sex must appear that a penis is going in and out of a vagina. blowjobs should have a penis going in and out of a womans mouth with head bob motion. sex from be hind penis enters and exits ass. if she is supposed to undress can you see her breasts. This is the most critical catagory.

WEIGHTED OVERALL SCORE:
  Subject Presence : 20%
  Motion Quality   : 20%
  POV Integrity    : 15%
  Prompt Alignment : 15%
  Scene Alignment  : 5%
  Continuity       : 5%
  Sexual Content Accuracy : 40%

PASS THRESHOLD: overall_score >= 8.0

SCENE CHANGE CONDITION (only if score < 5.0):
  For a very low score, Assess whether the story Scene Narrative can still be delivered comparing what is shown and what the desired SCENE NARRATIVE is, if there is too much deviation → set "scene_change_required" True, indicating a fresh cut scene transition is required since scene narrative can't be achieved in a continuous flow from the animation prompt

Gather your thoughts about why you scored the video this way compared to the animation prompt and scene narrative and return your 'reasoning' along with the other info and scores
Output strictly this JSON:
{{
    "reasoning":"basis and reasonining for the scores, what should be done to improve the scores via prompt changes",
    "subject_presence": 0,
    "motion_quality": 0,
    "pov_integrity": 0,
    "prompt_alignment": 0,
    "scene_alignment": 0,
    "continuity": 0,
    "sexual_content_accuracy": 0,
    "scene_change_required": false,
    "new_prompt": "a revised prompt that will be used to remake the clip and attempt to improve the scores."
}}

"""
    prompt =f"""Please review the video clip and associated animation prompt  "{animation_prompt}"., and anchor prompt for the scene "{anchor_image_prompt}". Return the JSON with scores, reasoning, and whether a scene change is required if the score is low and the new prompt that should be used to make the video based on the intent of the animation prompt and anchor prompt. Be honest and critical in your review to ensure high quality output. Return JSON only"""
    
    # Forwards the prompt to the VLM service to get an image prompt back
    service_type = 'vlm'
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'prompt': prompt, 'system_prompt': system_prompt, 'images': [], 'videos': [video_path], 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload} 
        )
    print(f"Response from VLM service: {response_data}")
    return response_data

def video_review3(video_path=None, image_path=None, system_prompt=None, user_prompt=None, save_path='outputs', project_id='default_project', prevent_model_unload=True):
    """
    scene is the entire scene dict
    scene narrative is just the single sentence overview for the scene
    
    """
    video_input = []
    if video_path is not None:
        video_input = [video_path]
    image_input = []
    if image_path is not None:
        image_input = [image_path]

    # Forwards the prompt to the VLM service to get an image prompt back
    service_type = 'vlm'
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'prompt': user_prompt, 'system_prompt': system_prompt, 'images': image_input, 'videos': video_input, 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload} 
        )
    print(f"Response from VLM service: {response_data}")
    return response_data


def generate_narrative(narrative_seed="",image_path=None, save_path='outputs', project_id='default_project', prevent_model_unload=True):
    """Use the vlm model to generate a high-level narrative structure with  progressive scene concepts based on the original image. This creates the overall story arc that will later be broken down into detailed animations.
    """
    system_prompt = f"""You are a creative director analyzing a portrait photograph to develop a short
interactive XXX narrative experience. You create high-level scene concepts for adult pornographic content starting from an image and/or idea. You generate a NARRATIVE STRUCTURE — not detailed shot descriptions.

Examine this image carefully and extract:
- Approximate age, gender presentation, and physical appearance
- Inferred personality traits, mood, and energy from facial expression,
  posture, styling, and environment
- The most believable relationship dynamic this person could have with the
  viewer 

Based on this character reading, construct a first-person POV narrative
encounter. The viewer is present with this person in a
shared space. The story should feel grounded and believable given who this
person appears to be.

CRITICAL: You are writing SCENE CONCEPTS, not animation instructions. Think of this as a story outline that will be broken into detailed shots LATER by an animation AI.

PERSPECTIVE: POV from viewer (male) interacting with a woman. Focus on what she does and how she acts.

SCENE STRUCTURE (progressive scenes for a short pornographic movie):
1. OPENING/FLIRTING: Initial interaction, teasing, establishing mood
2. UNDRESSING/BUILD-UP: Removing clothing, revealing body, building intimacy
3. SEXUAL ACTS: Primary sexual activity (blowjob, sexual intercourse, etc.)
3. SEXUAL ACTS: Secondary sexual activity (blowjob, sexual intercourse, etc.)
4. CLIMAX: Ejaculation and immediate reaction
5. COMPLETION: Aftermath and conclusion


✗ WRONG (too specific, animation-level details):
  - "leans forward slightly" ← body position detail
  - "leans back" ← body position detail
  - "licks the cum" ← specific action detail
  - "fingers tracing his spine" ← hand position detail
  - "eyes fluttering shut" ← facial detail
  - "arches into the release" ← body movement detail
  - "licking it off her fingers with a grin" ← action + expression detail
  - "hips swaying as she touches her waist" ← movement detail

FORBIDDEN SPECIFICS:
- NO specific body positions: "leans", "bends", "arches", "spreads", "lifts"
- NO specific actions with cum: "licks", "rubs", "wipes", "smears" → use "interacts with" or "plays with"
- NO emotional descriptors: "with satisfaction", "playfully", "teasingly"
- NO movement adverbs: "slowly", "deliberately", "sensually"

CONTENT RULES:
- Use explicit anatomical language (penis, vagina, breasts, nipples, blowjob, intercourse, ejaculation)
- Keep each scene to 2-3 sentences describing ONLY what general activity occurs
- State the activity, nothing more
- NO micro-actions or choreography

OUTPUT: Return a JSON object with numbered scenes. Each scene is a brief concept (2-3 sentences).

EXAMPLE FORMAT (DO NOT COPY - analyze the actual image and create original content):
{{
  "num": "scene activity",
  ...
  "num": "scene activity",
}}

USER WILL GIVE YOU IMAGE and STYLE EXAMPLES (different scenarios - use as abstraction guide, not content to copy)
"""
    
    if narrative_seed != "":
        narrative_seed = f" And this this narrative concept idea: {narrative_seed}."
    prompt = f"""Based on the Image {narrative_seed}, create a high-level narrative structure for a short adult film. The woman in the image is the main subject.

Analyze the image to determine:
- What is she wearing?
- What is the setting/location?
- What is her appearance/style?

Create ORIGINAL scene concepts with natural progression:
-  OPENING - What is the general opening activity? (flirting, teasing)
-  UNDRESSING - What specific clothing items is she wearing that get removed?
-  SEXUAL ACT - What is the primary sexual activity? (blowjob, etc.)
-  SEXUAL ACT - What is the secondary sexual activity? (missionary, doggystyle, cowgirl)
-  CLIMAX - Where does ejaculation occur? (face, breasts, body)
-  AFTERMATH - What happens after? (cum interaction, part ways)

CRITICAL RULES:
- CREATE ORIGINAL CONTENT based on the image - do not copy examples
- State ONLY the general activity or action per scene
- NO descriptions of HOW movements are performed
- NO body position adverbs: avoid "leans", "bends", "arches"
- specific cum interactions after climax
- NO emotional words: avoid "playfully", "teasingly", "with satisfaction"
- Keep to 2-3 brief sentences per scene
- Use explicit anatomical language (penis, vagina, ass)
- Use 'viewer' or 'camera' for context of actions
- Positions should make sense relative to the action. These poses and sex acts go together
    -- She is Kneeling for BLOWJOB and ORAL SEX
    -- She is laying on back for MISSIONARY sex
    -- She is squating above him for COWGIRL sex
    -- she is on hands and knees (tabletop pose) for DOGGYSTYLE sex

{narrative_seed}

RETURN only valid JSON with numbered keys "1", "2", "3", ... and scene concept values.
{{
  "num": "scene activity",
  ...
  "num": "scene activity",
}}
"""
    service_type = 'vlm'
     # Forwards the prompt to the VLM service to get an image prompt back
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'prompt': prompt, 'system_prompt': system_prompt, 'images': [image_path], 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload} 
        )
    print(f"Response from VLM service: {response_data}")
    return response_data

def generate_scene_animation_prompts(narrative=None, image_path=None, video_path=None,next_narrative=None,save_path='outputs', project_id='default_project', prevent_model_unload=True):
    """
    Takes a narrative concept, breaks it in to 2 specific animation prompts with anchor images
    """
    global WAN_LORA_FILES_MAP
    system_prompt = f"""You are a cinematographer designing shot-by-shot animation sequences for first-person POV adult films.

You will receive:
1. An input image or video showing the character in a scene
2. A scene concept narrative to break into animation clips

For each clip, generate:
- animation_prompt: Describe the motion, camera angle, and subject behavior
- anchor_image_prompt: Describe the END FRAME - what should be visible when the 4-second animation completes

ANIMATION PROMPT RULES:
- Motion must be naturalistic and flow naturally from clip to clip
- Use explicit language for adult content (penis, vagina, blowjob, intercourse, etc.)
- Present tense. Active verbs. No passive voice.
- One action only — do not stack multiple movements in a single prompt.
- No adjective-only stacks. Every sentence needs a motion verb.
- No re-description of the image (appearance, setting).
- No preamble, no labels, no explanation.
- 1 sentence

ANIMATION Low Rank Adapter (LoRA) Helpers
ONLY choose one of these LORA's {', '.join(WAN_LORA_FILES_MAP.keys())}
Lora is added to the prompt to help animate specific actions

ANCHOR IMAGE PURPOSE:
    Anchor images are single-frame references that preserve the character's face and identity while showing the END STATE of what the video prompt describes. They guide the I2V model on body position, camera angle, nudity state, and sexual interactions.

    YOUR TASK:
    For EACH animation prompt, generate a corresponding anchor image prompt.

    ANCHOR IMAGE RULES:
    1. ALWAYS START WITH: "Keep the person's face and identity exactly the same."
    2. DESCRIBE THE END FRAME: What should be visible at the END of the 4-second animation?
    3. MATCH THE CAMERA ANGLE from the video prompt:
    - "high angle looking down" → "high angle view"
    - "low angle looking up" → "low angle view"  
    - "eye level" → "eye level view"
    4. PRESERVE NUDITY STATE: If the video prompt removes clothing, the anchor shows them naked. MUST state she is 'naked' and what anatomy is visible (breasts, nipples,vagina, ass) DO NOT add anatomy that shouldn't be seen from the view angle.
    5. SHOW INTERACTIONS: If a penis enters frame or mouth, the anchor must show the penis in that position. If clothing is removed frame must state what anatomy is now visible and state nude and naked
    6. ALWAYS INCLUDES: "Sharp skin texture and hair detail."

    CAMERA ANGLE REFERENCE:
    - Blowjob → "high angle view POV looking down at her face, penis at bottom of frame entering her mouth"
    - Missionary sex → "high angle POV looking down at her vagina and breasts, penis at bottom of frame entering vagina"
    - Cowgirl sex → "low angle looking up at her ass, she is lowering onto erect penis"
    - Reverse Cowgirl → "low angle looking up at her breasts and face"
    - Doggystyle → "high angle looking at her back and ass, penis entering from behind"
    - Kneeling/lowering → "high angle medium shot looking down at her face and chest"
    - Standing/seated → "eye level view looking at her"

    LORA USE REFERENCE
    - ejaculation  → any scene with cum, seman or ejaculation on to the woman's face, breasts, chests, body, stomach, etc or in to her mouth
    - doggystyle  → any scene with doggystyle, where woman is on hands and knees or all fours and man is behind her his penis entering vagina from behind
    - cowgirl  → any scene where woman is above man penis is sticking up in to her vagina she is stradleing him while he lays down in cowgirl sex position
    - missionary  → any scene where she is laying down, man's penis is entering vagina and having sex in a missionary style
    - basic  → standard lora placeholder when no special lora is needed

    ANCHOR EXAMPLES:
    Undressing prompt → "Keep the person's face and identity exactly the same. Remove her clothing, show her nude, her breasts and nipples visible, she is naked. Eye level view. Sharp skin texture and hair detail."

    Lowering to kneel → "Keep the person's face and identity exactly the same. Repose her kneeling, looking slightly up toward viewer, high angle medium shot — downward perspective on her face, chest, and thighs. Sharp skin and hair detail. Sharp focus on her face."

    Penis enters frame (intro) → "Keep the person's face and identity exactly the same. She is kneeling, a penis enters from the bottom edge of the frame positioned near her face, high angle view looking down. Sharp skin and hair detail."

    Blowjob → "Keep the person's face and identity exactly the same. Add a penis at the bottom of the frame entering her mouth, blowjob, high angle view POV looking down at her face. Sharp skin and hair detail."

    Missionary sex → "Keep the person's face and identity exactly the same. Repose her having missionary vaginal intercourse, high angle POV looking at her vagina, navel and breasts visible. A penis at bottom of frame entering her vagina. Sharp skin and hair detail."


BASIC ANIMATION PROMPT STRUCTURE:
Prompt = Motion Description + Camera Movement
Motion Description: Describe the motion of elements in your image (e.g., subject woman), such as "kneeling" or "spreading legs." or "removes clothing" You can use adverbs like "quickly" or "slowly" to control the pace and intensity of the action.
Camera Movement: If you have specific requirements for camera motion, you can control it using prompts like "dolly in" or "pan left." If you wish for the camera to remain still, you can emphasize this with the prompt "static shot" or "fixed shot."

OUTPUT: JSON object with numbered clips, each containing animation_prompt and anchor_image_prompt."""

    prompt = f"""You are a cinematographer and prompt engineer designing shot-by-shot animation
sequences for a first-person POV XXX adult short film. The scene is based around the input {'Image' if image_path else 'Video'}.

Take this scene concept: "{narrative}"

break scene concept in to two (2) animation clips.

THINKING:
When you prompt, think about how people move what body parts need to be mentionded so they are acted on, what body parts should not be mentioned to avoid confusion. Think about the woman subject like a puppet you need to specifically describe what you want her to do with her body and how the camera should move to capture it.
Be specific about the direction, like hand picks up item and moves it from left to right, or penis enters bottom of frame then penis enters her vagina, or she slides her pants down her legs on to the floor.

CRITICAL CONSTRAINTS:
- All shot angles (low or high) used to create a first-person POV experience. The camera IS the viewer's eyes watching the subject woman. use "high angle looking down" or "low angle looking up" to control the perspective and framing of the subject in relation to the viewer's position.
- Motion must be naturalistic — no floating, teleporting, or impossible movement.
- Each clip must flow from the previous via the continuity note.
- Use explicit language for adult content (penis enters vagina, penis enters mouth, penis enters ass)
- Use explicit language for undressing and striptease (removes clothing breasts and nipples visible, removes pants vagina visible) must state the nude body parts under the clothing so they render

For each clip output this structure inside a "clips" array:
clip_number = {{
  "animation_prompt": "Woman subject behavior, Detailed description of motion, camera behavior, and Be specific about direction, speed, and feel.",
  "anchor_image_prompt": "Detailed still image prompt of the key frame for this clip. Include lighting, composition, subject expression, environment. IT IS CRITICAL that your anchor image is related to the result of the animation_prompt",
  "lora": "One of these based on prompt: {', '.join(WAN_LORA_FILES_MAP.keys())}"
  }}

Output:
{{
  "1": shot json,
  "2": shot json,
}}

The next scene concept is:  "{next_narrative}" shot 2 should begin to lead towards that concept up. """

    service_type = 'vlm'
    if image_path is None:
        images = []
    else:
        images = [image_path]
    
    if video_path is None:
        videos = []
    else:
        videos = [video_path]

    # Forwards the prompt to the VLM service to get an image prompt back
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'prompt': prompt, 'system_prompt': system_prompt, 'images': images, 'videos': videos, 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload} 
        )
    print(f"Response from VLM service: {response_data}")
    return response_data

def generate_sfx_prompt(video_path=None, save_path='outputs', project_id='default_project', prevent_model_unload=True):
    """Use the vlm model to generate sound effects based on the generated narrative and scenes. The sound effects should be specific to the actions and events in each scene, such as moaning, bed creaking, or other ambient sounds that would enhance the realism and immersion of the video.
    """
    service_type = 'vlm'
    sfx_prompt = """I need you to describe and come up with a short sound effect description for the video which will be used as a prompt for a video to sound AI model. 
    You are creating the sound prompt for the scenes.
    1. Add key sounds effects that would enhance the atmosphere.
    2. Use descriptive language to convey the intensity and mood of the scene.
    3. Keep it concise.
    4. If it is a sexual scene Include specific sexual sound elements like 'moans, breathing, orgasim, ejaculation, sucking, vaginal intercourse, thrusting, slapping, gagging, deepthroat, kissing, laughing, giggling, whispering, blowjob, intercourse, skin contact, etc.'
    5. Avoid generic terms.
    6. Keep the description of the sounds in temporal or time order.
    7. You can create some ambient background sounds if it fits the scene, like nature sounds, city noises, ocean waves, etc. at the end of the prompt
    8. Ensure the prompt is appropriate for generating sound effects only. DO NOT add music or songs.
    10. Graphic sexual sounds are encouraged to match the explicit nature of some scenes (blowjob, intercourse, thrusting, moaning).

    Use clear and descriptive keywords to specify the desired sounds. For example, instead of just "Water," use "Gentle waves lapping against shore."

    ONLY return language text prompt, no special tags, characters, formatting, asterisks, or quotes.

    The prompt should have a temporal flow, describing the sounds in the order they occur in the scene. For example, "Soft moans and heavy breathing build up to a crescendo of orgasmic sounds, followed by the gentle rustling of sheets and a final sigh of satisfaction."

    Prompt should be 2 or less sentences, concise but descriptive of sound elements.
    """
    # Forwards the prompt to the VLM service to get an image prompt back
    service_type = 'vlm'
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'prompt': sfx_prompt, 'images': [], 'videos': [video_path], 'save_path': save_path, 'project_id': project_id, 'prevent_model_unload': prevent_model_unload} 
        )
    print(f"Response from {service_type} service: {response_data}")
   
    return response_data

def generate_sfx(sfx_prompt=None, video_path=None, save_path='outputs', project_id='default_project', prevent_model_unload=True):
    # Forwards the prompt to the VLM service to get an image prompt back
    service_type = 'sfx'
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'prompt': sfx_prompt,  'video_path': video_path, 'save_path': save_path, 'project_id': project_id} 
        )
    print(f"Response from {service_type} service: {response_data}")
    """
    {'count': 2, 
        'files_saved': [{'file_name': 'scene6_joined_clip_sfx.wav', 
        'mime_type': 'audio/wav', 
        'path': 'C:\\Users\\computer_user\\documents\\code\\haughtstudio\\projects\\auto_project\\test_run20\\base_images\\scene6_joined_clip_sfx.wav', 
        'relative_path': 'projects\\auto_project\\test_run20\\base_images\\scene6_joined_clip_sfx.wav', 
        'size_bytes': 661548}, 
        {'file_name': 'scene6_joined_clip_with_sfx.mp4', 
        'mime_type': 'video/mp4', 
        'path': 'C:\\Users\\computer_user\\documents\\code\\haughtstudio\\projects\\auto_project\\test_run20\\base_images\\scene6_joined_clip_with_sfx.mp4', 
        'relative_path': 'projects\\auto_project\\test_run20\\base_images\\scene6_joined_clip_with_sfx.mp4', 
        'size_bytes': 11262563}
        ], 
    'status': 'success'}
Generated Sound Effects: {'file_name': 'scene6_joined_clip_sfx.wav', 'mime_type': 'audio/wav', 'path': 'C:\\Users\\computer_user\\documents\\code\\haughtstudio\\projects\\auto_project\\test_run20\\base_images\\scene6_joined_clip_sfx.wav', 'relative_path': 'projects\\auto_project\\test_run20\\base_images\\scene6_joined_clip_sfx.wav', 'size_bytes': 661548}
    """
    response_data = {'sfx_path':response_data['files_saved'][0]['path'],
                     'video_path':response_data['files_saved'][1]['path']}

    return response_data


def generate_dialogue(narrative=None, scenes=None, save_path='output', project_id='default_project', prevent_model_unload=True):
    """Use the vlm model to generate dialogue based on the generated narrative. The dialogue should be specific to the characters and events in the narrative, and should enhance the storytelling and character development in the video.
    """
    service_type = 'vlm'
    dialogue_prompt = f"""The Image is the person who you will create a voice description and dialogue for. The story narrative is:\n{narrative}\n
    The dialogue is one directional it is the person in the Image speaking to viewer.
    Short flirtatious banter, brief expressions of desire, advance the narrative. 

    The dialogue should be short and explicit, not more than 10 words per scene.

    SCENES to add dialogue to:\n{scenes}\n

    The dialogue should be natural and flow well with the narrative, and should help to bring the characters to life and make the story more engaging for the viewer.

    DO NOT add dialogue to every scene. 'Scene Change' do not have dialogue. DO NOT add dialogue if a penis is in her mouth or face isn't visibile or something is in her mouth (blowjobs, doggystyle have no dialogue)

    Example Output:
    {{'1': 'Mmm, I love it when you watch me.',
     '2': 'Do you like what you see? I just got out of the shower.',
     '3': '',
     '4': 'Keep fucking me hard like that.',
     '5':'',
     '6': 'Mmm, that feels so good, I want you to cum inside me.'}}

    Return JSON with scene numbers as keys and the dialogue for that scene as values. 
    ONLY return the JSON with no additional text or explanation. 
    The JSON should be properly formatted and parsable.
    """
    print("\n\nWARNING: Dialogue generation DISABLED!\n\n")
    #create a blank object for each scene in scenes
    dialogue = {}
    for num,scene in scenes.items():
        dialogue[num] = ""
    return dialogue
    # Forwards the prompt to the VLM service to get an image prompt back
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            #universally Required arguments: save_path, project_id, service_type
            {'prompt': dialogue_prompt, 'images': [], 'save_path': save_path, 'project_id': project_id, 'service_type': service_type, 'prevent_model_unload': prevent_model_unload} 
        )
    print(f"Response from VLM service: {response_data}")
    print(f"Cleaned Dialogue for TTS:")
    # Clean dialogue for TTS compatibility by removing special Unicode characters
    return clean_dialogue_for_tts(response_data)

     

# ========================================
# SPEECH
# ========================================
def design_voice(instructions=None, save_path='output', project_id='default_project'):
    print("VOICE DESIGN")
    print(f"Instructions: {instructions},\n Save Path: {save_path},\n Project ID: {project_id}")
   
    # Forwards the dialogue to the TTS service will get audio file back to be reused to mimic this voice with an TTS request
    service_type = 'tts'

    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'instruct': instructions, 'return_designed_voice': True, 'save_path': save_path, 'project_id': project_id})
    print(f"Response from TTS service: {response_data}")
    return response_data['files_saved'][0]['path']
             
def text_to_speech(text="", voice_reference_file=None, voice_reference_transcript= None, instructions=None, file_name ="output.wav", save_path='output', project_id='default_project'):
    print("TEXT TO SPEECH GENERATION")
    print(f"Received text: {text},\n Voice Sample File: {voice_reference_file},\n Voice Sample Transcript: {voice_reference_transcript},\n Instructions: {instructions},\n Save Path: {save_path},\n Project ID: {project_id}")
   
    # Forwards the dialogue to the TTS service to get audio files back
    service_type = 'tts'

    text_segment =[{
            "text": text,
            "leading_silence": 0.0,#add a short leading silence 
            "ending_silence":0.0,#add a short ending silence 
        }]
    if voice_reference_file and voice_reference_transcript and instructions:
        print("WARNING: passing voice reference file, transcript, and instructions to TTS generation will result in the voice reference being ignored and a new voice being created with the instructions.")
        print("If you wanted to use voice reference DO NOT pass instructions argument, just pass the voice reference file and transcript. Instructions are only for creating a new voice without a reference.")

    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'segments': text_segment, 'voice_sample_file': voice_reference_file, 'voice_sample_transcript': voice_reference_transcript, 'instruct': instructions, 'output_file': file_name, 'save_path': save_path, 'project_id': project_id} 
        )
    print(f"Response from TTS service: {response_data}")
    return response_data

def generate_dialogue_from_video(dialogue_text=None, dialogue_video_prompt =None, source_video_path=None, voice_reference_file=None, voice_reference_transcript= None, anchor_image_path=None, file_suffix="dialogue", save_path='output', project_id='default_project'):
    """
    generates a lipsync video starting from a video or an image (can't be both, default is to use video if both are provided)
    Arguments:
        dialogue_text: the text of the dialogue to be spoken in the video
        dialogue_video_prompt: prompt used to animate the base video that will be lipsynced over
        source_video_path: the path to the source video to be used for lipsync
        voice_reference_file: the path to the audio file containing the reference WAV for the voice for the TTS model to mimic
        voice_reference_transcript: the transcript of the voice reference audio file, used to help the TTS model understand the voice characteristics to mimic
        anchor_image_path: the path to the anchor image to be used when source_video is provided
        file_suffix: the suffix to append to the generated file names
        save_path: the path to save the generated video
        project_id: the project ID to associate with the generated video
    Returns:
    A dictionary containing the paths to the generated silent video, the final lipsynced video, the last frame of the video, and the audio file used for lipsyncing.
        silent_video_path: the path to the generated silent video that was used as the base for lipsyncing
        video_path: the path to the final generated video with lipsyncing applied
        last_frame_path: the path to the last frame of the generated video, which can be used as a thumbnail or preview image
        audio_path: the path to the audio file that was used for lipsyncing, which can be useful for reference or reuse in other contexts
    """
    if not dialogue_video_prompt:
        print("No dialogue video prompt provided, using default prompt for video generation. CONTAINS SLOW MOTION MODIFIER")
        dialogue_video_prompt = "continue the motion. subject direct eye contact with viewer.filmed in ultra slomo slow-motion capture slow motion scene. "

    print(f"Generating TTS audio for dialogue video using transcript: {dialogue_text}")
    tts_result = text_to_speech(text=dialogue_text, 
                                voice_reference_file=voice_reference_file, voice_reference_transcript=voice_reference_transcript, file_name=f"{file_suffix}.wav", save_path=save_path, project_id=project_id)
    print(f"TTS result file path: {tts_result}")

    audio_file_path = tts_result['files_saved'][0]['path']
    
    response_data = None
    print(f"Source video path provided: {source_video_path}")
    if source_video_path:
        response_data = video_to_video(video_prompt=dialogue_video_prompt, 
                                       video_path=source_video_path, 
                                       anchor_image_path=anchor_image_path,
                                       file_suffix=file_suffix, 
                                       save_path=save_path, 
                                       project_id=project_id)
        
        print(f"Response from v2v service: {response_data}")
        #set the source video to the newly created clip, which will now be used to lipsync over
        source_video_path = response_data['clip_video_path']
        video_path_silent = source_video_path
        print(f"created clip video to be used for lipsync: {video_path_silent}")

   
    else:
        print("No source video provided for dialogue lipsync generation.")
        return response_data
    
    #Now do the lipsync using the generated TTS audio and the source video or generated clip from image
    print(f"Using source video {source_video_path} and audio {audio_file_path} for lipsync generation.")
    result = video_lipsync(video_path=source_video_path, audio_path=audio_file_path, file_name=file_suffix, save_path=save_path, project_id=project_id)
    print(f"Response from Video Lipsync service: {result}")

    #We created a clip, then used that clip to lipsync over, now we return the final lipsyned video path and associated files.
    final_result = {'silent_video_path': result['silent_video_path'], 
                    'video_path': result['video_path'], 
                    'last_frame_path': result['last_frame_path'], 
                    'audio_path': audio_file_path}

    return final_result

def generate_dialogue_from_image(dialogue_text=None,dialogue_video_prompt =None,  voice_reference_file=None, voice_reference_transcript= None, source_image_path=None, file_suffix="dialogue", save_path='output', project_id='default_project'):
    """
    generates a lipsync video starting from a video or an image (can't be both, default is to use video if both are provided)
    Arguments:
        dialogue_text: the text of the dialogue to be spoken in the video
        dialogue_video_prompt: prompt used to animate the base video that will be lipsynced over
        voice_reference_file: the path to the audio file containing the reference WAV for the voice for the TTS model to mimic
        voice_reference_transcript: the transcript of the voice reference audio file, used to help the TTS model understand the voice characteristics to mimic
        source_image_path: the path to the source image to be used for lipsync if no video is provided
        file_suffix: the suffix to append to the generated file names
        save_path: the path to save the generated video
        project_id: the project ID to associate with the generated video
    Returns:
    A dictionary containing the paths to the generated silent video, the final lipsynced video, the last frame of the video, and the audio file used for lipsyncing.
        silent_video_path: the path to the generated silent video that was used as the base for lipsyncing
        video_path: the path to the final generated video with lipsyncing applied
        last_frame_path: the path to the last frame of the generated video, which can be used as a thumbnail or preview image
        audio_path: the path to the audio file that was used for lipsyncing, which can be useful for reference or reuse in other contexts
    """

    
    if not dialogue_video_prompt:
        print("No dialogue video prompt provided, using default prompt for video generation. CONTAINS SLOW MOTION MODIFIER")
        dialogue_video_prompt = "continue the motion. subject direct eye contact with viewer. filmed in ultra slomo slow-motion capture slow motion scene. "

    print(f"Generating TTS audio for dialogue video using transcript: {dialogue_text}")
    tts_result = text_to_speech(text=dialogue_text, 
                                voice_reference_file=voice_reference_file, 
                                voice_reference_transcript=voice_reference_transcript, 
                                file_name=f"{file_suffix}.wav", 
                                save_path=save_path, 
                                project_id=project_id)
    
    print(f"TTS result file path: {tts_result}")

    audio_file_path = tts_result['files_saved'][0]['path']
    
    response_data = None

    if source_image_path:
        response_data = image_to_video(image_prompt=dialogue_video_prompt, image_path=source_image_path, file_suffix=file_suffix, save_path=save_path, project_id=project_id)
        print(f"Response from i2v service: {response_data}")
        source_video_path = response_data['video_path']
        video_path_silent = source_video_path
        #we now have a base video clip to lipsync over.
        print(f"created clip video for lipsync from image: {video_path_silent}")
    
    else:
        print("No source image provided for dialogue lipsync generation.")
        return response_data
    
    #Now do the lipsync using the generated TTS audio and the source video or generated clip from image
    print(f"Using source video {source_video_path} and audio {audio_file_path} for lipsync generation.")
    result = video_lipsync(video_path=source_video_path, audio_path=audio_file_path, file_name=file_suffix, save_path=save_path, project_id=project_id)
    print(f"Response from Video Lipsync service: {result}")

    final_result = {'silent_video_path': result['silent_video_path'], 
                    'video_path': result['video_path'], 
                    'last_frame_path': result['last_frame_path'], 
                    'audio_path': audio_file_path}

    return final_result

# ========================================
# STYLE PROMPTS
# ========================================
def prompt_flirting(breast_description="breasts", clothing_description="wearing a silk slip dress", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a flirting/teasing opening scene."""

    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for opening flirting and teasing scenes.
    You will be given an image or video of the current scene. Your task is to create an animation prompts flirtatious sequence.
    The woman should be engaging with the camera in a suggestive but not yet explicit way.
    Review the input image or video to determine the scene state

    SCENE STATE:
    - Clothing: what they are wearing, how revealing it is, how it moves on the body, what body parts are visible through or around the clothing
    - Setting: what is the environment, is it indoor or outdoor, what objects are around, what is the furniture, what is the lighting like
    - Activity: engaged in an activity, are they making eye contact, are they smiling, are they touching their hair, are they looking at the camera, are they looking away from the camera, are they talking, are they moving their body in a certain way
    - Position: sitting, standing, laying down, kneeling, etc

    Return a JSON with three (4) keys:
    'reasoning': A brief explanation of the thought process behind the prompt, describing why you chose the specific actions and camera movements to create a flirtatious atmosphere.
    'prompt': A prompt that will animate the next clip of the flirting scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene, describing the specific sounds that would enhance the flirtatious atmosphere and match the actions in the video.
    'dialogue': '(OPTIONAL) A short piece of flirtatious dialogue that the character would say to the viewer in this scene, which will be used for TTS generation. The dialogue should be concise, explicit, and enhance the flirtatious mood of the scene. This is optional, can return empty string if no dialogue is desired for this clip, but if included should be 10 words or less to keep it punchy and natural for a flirtatious opening scene.'
    
    """
    user_prompt = f"""Create a 5-second flirting/teasing prompt and sound effects prompt and consider optional dialogue for TTS generation.

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Simple descriptions of HOW movements are performed  (hand raises to hair, eyes look directly at camera, hand picks up wine glass, extends arm and points index finger at viewer,etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- Keep to 2 motion focused brief sentences
- Focus on subtle body language: eye contact, hair touching, lip biting, body turning, laughing, smiling, etc. that conveys flirtation and teasing without being explicit
- Use explicit anatomical language when body parts are touched
- Use 'viewer' or 'camera' for context
- This is a NON-EXPLICIT opening scene, clothing stays ON
- As needed mention the woman's breasts to make sure they animate correctly: {breast_description}
- Focus on her clothing as needed to make sure it animates correctly: {clothing_description}
- Focus on the eye color as needed to make sure it animates correctly: {eye_color}

Reasoning is a short explination of what you think is happening and why you chose to prompt the woman to do the specific action in the prompt.
prompt is the actual motion focused brief sentences.

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context, matching the actions in the scene describing the sounds as the happen.

DIALOGUE:
A required key, BUT not all scenes would have talking. If it seems appropriate add a short no more than 10 word pharse for the woman to say. Else empty string

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string or empty string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}


def prompt_striptease(breast_description="breasts", clothing_description="wearing a silk slip dress", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a striptease scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for striptease and undressing scenes.
    You will be given an image or video input, it will show the scene state including what clothing is being worn. Your task is to create animation and anchor prompts for a gradual undressing sequence.
    Review the input image or video to determine how the scene should progress.  If she is fully clothed DO NOT have her remove all clothing in one prompt, that will come later, the prompt should only tease and remove one, top or bottoms. 
    Return a JSON with three (4) keys:
    'reasoning': A brief explanation of the thought process behind the prompt, describing why you chose the specific actions and camera movements to create a flirtatious atmosphere.
    'prompt': A prompt that will animate the next clip of the flirting scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene, describing the specific sounds that would enhance the flirtatious atmosphere and match the actions in the video.
    'dialogue': '(OPTIONAL) A short piece of flirtatious dialogue that the character would say to the viewer in this scene, which will be used for TTS generation. The dialogue should be concise, explicit, and enhance the flirtatious mood of the scene. This is optional, can return empty string if no dialogue is desired for this clip, but if included should be 10 words or less to keep it punchy and natural for a flirtatious opening scene.'
    
    """
    user_prompt = f"""Review the input video or Image. Create a 5-second striptease/undressing scene clip prompt.

- Original Outfit: {clothing_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Descriptions of HOW she moves and how the clothing is removed (example: 'fingers unbutton bra at center, moves arms through the straps and drops bra on floor, her naked {breast_description} are visible' or 'hands on her hips slide her panties down her thighs, sand removes them casting the panties out of frame, her vagina is visible', etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- Explicitly state what clothing remains AND what anatomy is now visible after removing
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language when body parts are revealed after clothing is removed (breasts, nipples, ass, vagina)
- Use 'viewer' or 'camera' for context because this is a POV (point of view) filmed scene
- Focus on the MECHANICS of removing clothing and teasing actions

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context, matching the actions in the scene describing the sounds as the happen.

DIALOGUE:
A required key, BUT not all scenes would have talking. If it seems appropriate add a short no more than 10 word pharse for the woman to say. Else empty string

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string or empty string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}

def prompt_dirtyDance(breast_description="breasts", clothing_description="wearing a silk slip dress", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a dirty dancing scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for dirty dancing scenes.
    You will be given an image or video input. Your task is to create animation prompts for a sexy, high-energy dancing sequence.
    The woman should be moving rhythmically, twirling, grinding, or displaying her body to the camera.
    
    Return a JSON with four (4) keys:
    'reasoning': A brief explanation of the thought process.
    'prompt': A prompt that will animate the next clip of the dancing scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene.
    'dialogue': '(OPTIONAL) Short dirty talk, laughter, or moans.'
    """
    user_prompt = f"""Review the input video or Image. Create a 5-second dirty dancing scene clip prompt.

SCENE STATE:
- Outfit: Nude
- Eyes: {eye_color}
- Breasts: {breast_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Focus on DANCING actions: twirling, spinning, grinding, swaying, shaking ass, arching back, bending over
- Simple descriptions of HOW movements are performed EXAMPLES: (hips rotate in circle, arms raise above head, spins body around showing ass, hands tracing curves of body, kneels down then stands back up. arches back pushes chest forward then stands up straight, turns around her hands spread her ass cheeks apart)
- Describe the MOVEMENT of the body (e.g., 'back curves deeply', 'torso lowers towards floor') rather than static poses.
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language (ass, hips, thighs, breasts, legs, waist)
- Use 'viewer' or 'camera' for context
- SHE IS DANCING: Make it dynamic and seductive.
- Include in the prompt 'zoomed out full body view' to make sure we show her entire body.

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context (e.g., 'rhythmic bass pulsing', 'swish of dress', 'heels clicking on floor').

DIALOGUE:
A required key. If appropriate, add a short phrase (max 10 words) for the woman to say. Else empty string.

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}


def prompt_blowjob(breast_description="breasts", clothing_description="naked", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a blowjob/oral sex scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for blowjob/oral sex scenes.
    You will be given context or an image input. Your task is to create animation prompts specifically for oral sex scenes.
    The woman should be kneeling with the camera looking down at her (POV style).
    If an image is provided, use it as the starting state.
    
    Return a JSON with four (4) keys:
    'reasoning': A brief explanation of the thought process behind the prompt.
    'prompt': A prompt that will animate the next clip of the blowjob scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene.
    'dialogue': '(OPTIONAL) Since the mouth is occupied, this should usually be an empty string unless she pulls away to speak.'
    """
    user_prompt = f"""Create a 5-second blowjob/oral sex scene clip prompt.

SCENE STATE:
- Clothing: Nude
- Position: Woman is kneeling, man standing (POV from man's perspective)
- Eyes: {eye_color}
- Breasts: {breast_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Simple descriptions of HOW movements are performed (head moves forward, mouth opens, hand grips penis shaft, etc.) some words like 'shaft' or 'head' need context make sure to include the anatomical context like Penis in those examples
- NO body position adverbs: avoid "leans", "bends", "arches"
- NO 'licking' or NO 'tounge' NO adverbs: avoid "licks", "swirls tongue around", "tongue traces", etc. The model CAN NOT animate that type of mouth penis interaction. Instead describe the motion of the head and mouth and use explicit anatomical language (e.g., 'head moves up and down on penis', 'a hand comes in from out of frame and placed on the back of her head'),
- NO emotional words: avoid "playfully", "teasingly", "with satisfaction"
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language (penis, mouth, lips, penis shaft, hand)
- Use 'viewer' or 'camera' for context
- She is KNEELING for this oral sex scene
- USE 'direct eye contact' or 'looking at camera' or 'intense stare at viewer'
- USE speed instructions as you see fit to indicate how quickly or slowly motion happens..

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context (e.g., 'wet slurping noises', 'gags', 'moans').

DIALOGUE:
Usually empty string as mouth is occupied.

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}


def prompt_missionary(breast_description="breasts", clothing_description="naked", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a missionary sex scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for missionary sex scenes.
    You will be given context or an image input. Your task is to create animation prompts specifically for missionary position scenes.
    The woman should be laying on her back with legs apart, the man on top or the camera looking down at her.
    
    Return a JSON with four (4) keys:
    'reasoning': A brief explanation of the thought process.
    'prompt': A prompt that will animate the next clip of the missionary scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene.
    'dialogue': '(OPTIONAL) Short dirty talk or moans.'
    """
    user_prompt = f"""Create a 5-second missionary sex scene clip prompt.

SCENE STATE:
- Clothing: {clothing_description}
- Position: Woman is laying on her back, man on top (POV from man's perspective)
- Eyes: {eye_color}
- Breasts: {breast_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Simple descriptions of HOW movements are performed (hips thrust forward, legs wrap around, body pushes down, etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- NO emotional words: avoid "playfully", "teasingly", "with satisfaction"
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language (penis, vagina, breasts, nipples, thighs, hips)
- Use 'viewer' or 'camera' for context
- She is LAYING ON HER BACK for this missionary scene

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context (e.g., 'slapping of skin', 'heavy breathing', 'moans').

DIALOGUE:
A required key. If appropriate, add a short phrase (max 10 words) for the woman to say. Else empty string.

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}


def prompt_cowgirl(breast_description="breasts", clothing_description="naked", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a cowgirl sex scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for cowgirl sex scenes.
    You will be given context or an image input. Your task is to create animation prompts specifically for cowgirl position scenes.
    The woman should be on top, straddling the man who is laying down, with the camera looking up at her.
    
    Return a JSON with four (4) keys:
    'reasoning': A brief explanation of the thought process.
    'prompt': A prompt that will animate the next clip of the cowgirl scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene.
    'dialogue': '(OPTIONAL) Short dirty talk or moans.'
    """
    user_prompt = f"""Create a 5-second cowgirl sex scene clip prompt.

SCENE STATE:
- Clothing: {clothing_description}
- Position: Woman is on top straddling man, squatting above him, man laying down (POV from man looking up)
- Eyes: {eye_color}
- Breasts: {breast_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Simple descriptions of HOW movements are performed (hips rise up, body drops down, hands press on chest, etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- NO emotional words: avoid "playfully", "teasingly", "with satisfaction"
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language (penis, vagina, breasts, nipples, hips, thighs)
- Use 'viewer' or 'camera' for context
- She is SQUATTING ABOVE HIM for this cowgirl scene

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context.

DIALOGUE:
A required key. If appropriate, add a short phrase (max 10 words) for the woman to say. Else empty string.

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}


def prompt_reverse_cowgirl(breast_description="breasts", clothing_description="naked", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a reverse cowgirl sex scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for reverse cowgirl sex scenes.
    You will be given context or an image input. Your task is to create animation prompts specifically for reverse cowgirl position scenes.
    The woman should be on top facing away from the man, with her back and ass visible to camera.
    
    Return a JSON with four (4) keys:
    'reasoning': A brief explanation of the thought process.
    'prompt': A prompt that will animate the next clip of the reverse cowgirl scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene.
    'dialogue': '(OPTIONAL) Short dirty talk or moans.'
    """
    user_prompt = f"""Create a 5-second reverse cowgirl sex scene clip prompt.

SCENE STATE:
- Clothing: {clothing_description}
- Position: Woman is on top facing AWAY from man, her back and ass visible to camera, man laying down
- Eyes: {eye_color}
- Breasts: {breast_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Simple descriptions of HOW movements are performed (hips rise up, body drops down, back muscles flex, etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- NO emotional words: avoid "playfully", "teasingly", "with satisfaction"
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language (penis, vagina, ass, back, hips, thighs)
- Use 'viewer' or 'camera' for context
- She is FACING AWAY on top for this reverse cowgirl scene
- Her back, ass, and the penetration point are the focal elements

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context.

DIALOGUE:
A required key. If appropriate, add a short phrase (max 10 words) for the woman to say. Else empty string.

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}


def prompt_doggystyle(breast_description="breasts", clothing_description="naked", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a doggystyle sex scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for doggystyle sex scenes.
    You will be given context or an image input. Your task is to create animation prompts specifically for doggystyle position scenes.
    The woman should be on hands and knees (tabletop pose), man behind her.
    
    Return a JSON with four (4) keys:
    'reasoning': A brief explanation of the thought process.
    'prompt': A prompt that will animate the next clip of the doggystyle scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene.
    'dialogue': '(OPTIONAL) Short dirty talk or moans.'
    """
    user_prompt = f"""Create a 5-second doggystyle sex scene clip prompt.

SCENE STATE:
- Clothing: {clothing_description}
- Position: Woman on hands and knees (tabletop pose), man behind her (POV from behind)
- Eyes: {eye_color}
- Breasts: {breast_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Simple descriptions of HOW movements are performed (hips push back, body rocks forward, hands grip hips, etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- NO emotional words: avoid "playfully", "teasingly", "with satisfaction"
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language (penis, vagina, ass, hips, back, breasts)
- Use 'viewer' or 'camera' for context
- She is ON HANDS AND KNEES for this doggystyle scene

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context.

DIALOGUE:
A required key. If appropriate, add a short phrase (max 10 words) for the woman to say. Else empty string.

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}


def prompt_facial(breast_description="breasts", clothing_description="naked", eye_color="blue eyes"):
    """Returns (system_prompt, user_prompt) for a facial/cumshot scene."""
    
    system_prompt = f"""You are a pornographic XXX adult film directors assistant that creates prompts for facial and cumshot scenes.
    You will be given context or an image input. Your task is to create animation prompts specifically for the climax of the scene where the man ejaculates on the woman's face or body.
    
    Return a JSON with four (4) keys:
    'reasoning': A brief explanation of the thought process.
    'prompt': A prompt that will animate the next clip of the facial scene.
    'sound_effects': A prompt that will be used to generate sound effects for the scene.
    'dialogue': '(OPTIONAL) Short dirty talk, moans, or reactions to the facial.'
    """
    user_prompt = f"""Create a 5-second facial/cumshot scene clip prompt.

SCENE STATE:
- Clothing: {clothing_description}
- Position: Woman is kneeling or looking up at camera, close up on face
- Eyes: {eye_color}
- Breasts: {breast_description}

CRITICAL RULES:
- State ONLY the action for the next 5 seconds
- Simple descriptions of HOW movements are performed (eyes close, tongue sticks out, mouth opens, her hand rubs semen on skin, etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- Depict the moment of ejaculation how her facial expression reacts or the immediate aftermath dripping, wiping with fingers, swallowing etc.
- Use explicit anatomical language (semen, cum, face, eyes, mouth, tongue, skin)
- Use 'viewer' or 'camera' for context this is a POV point of view scene
- Focus on the FACIAL REACTION and the application of semen
- CRITICAL - if there is NO white fluid, seman, or cum visible in the scene yet the prompt should have the hand masturbating the penis first, stroking multiple times and THEN ejaculation.  IF you already see white fluid, seman or cum than contine to prompt her interaction with it

SOUND EFFECTS:
A single sentence that describes the sound environment in a temporal context (e.g., 'splattering sounds', 'heavy breathing', 'moans of satisfaction', 'Gulping').

DIALOGUE:
A required key. If appropriate, add a short phrase (max 10 words) for the woman to say. Else empty string.

Return ONLY a JSON schema:
{{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
"""
    return {'system_prompt': system_prompt, 'user_prompt': user_prompt}

"""
COMFYUI SERVICE FUNCTIONS
"""        
# ========================================
# TEXT TO IMAGE
# ========================================
def text_to_image(image_prompt, save_path, project_id, sub_folder=None,lora= None, width=1280, height=960):
    print("TEXT TO IMAGE GENERATION")
    print(f"Received image prompt: {image_prompt}")
    if sub_folder:
        print(f"Sub folder specified: {sub_folder}, adding to save path.")
        save_path = os.path.join(save_path, sub_folder)
    print(f"Save path: {save_path},\n Project ID: {project_id},\n Sub Folder: {sub_folder}, \nWidth: {width}, Height: {height}")
    # Forwards the image prompt to the ComfyUI service to generate an image
    service_type = 't2i'
    #Get the workflow for this endpoint. For now we can hardcode it, but eventually we may want to allow users to specify different workflows for different endpoints.
    t2i_workflow_path = WORKFLOWS[service_type]
    with open(t2i_workflow_path, "r") as f:
        workflow = json.load(f)
    
    inputNodes = "131"
    noiseNodes = "13"
    dimensionNodes = "5"
    saveNodeRaw = "57"#f4f/image_960
    saveNodeRaw480 = "135"#f4f/image_raw_480

    #assign workflow variables
    #noise seed
    seed = random.randint(1, 1000000000)
    workflow[noiseNodes]["inputs"]["noise_seed"] = seed
    #text pompt input
    #SDXL text encoder uses an 'L' and a 'G' input, where 'L' is used as a seconday prompt for style
    workflow[inputNodes]["inputs"]["text_g"] = image_prompt
    workflow[inputNodes]["inputs"]["text_l"] = image_prompt #just duplicate the prompt for now, search best way SDXL uses G and L clip prompts
    
    #output dimensions
    workflow[dimensionNodes]["inputs"]["width"] = width
    workflow[dimensionNodes]["inputs"]["height"] = height

    #save path comfyui uses - don't worry about this, we will map the final desired file name to the output node in the workflow and then rename the file after generation
    raw = workflow[saveNodeRaw]["inputs"]["filename_prefix"] #raw image
    raw_480 = workflow[saveNodeRaw480]["inputs"]["filename_prefix"] #raw 480 image


    #map the desired file name to the output nodes in the workflow
    base_filename = f"{project_id}_base_1280x960.png"
    file_prefix = {saveNodeRaw: base_filename, 
                   saveNodeRaw480: f"{project_id}_resize_480p.png"}

    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'workflow': workflow, 'save_path': save_path, 'project_id': project_id, 'file_prefix': file_prefix, 'service_type': service_type} 
        )

    print(f"\n\nResponse from ComfyUI service:\n {response_data}")
    print(f"ONLY returning the enhanced full image")
    if success and 'files_saved' in response_data:
        enhanced_full_path = None
        for file_info in response_data['files_saved']:
            if file_info['file_name'] == base_filename:
                enhanced_full_path = file_info['path']
                break
        
        if enhanced_full_path:
            print(f"Enhanced full image saved at: {enhanced_full_path}")
            return {'image_path':enhanced_full_path,'noise_seed': seed}
        else:
            print("Enhanced full image not found in response.")
            return {'image_path':None,'noise_seed': None}
    else:
        print("Image generation failed or no files saved.")
        return {'image_path':None,'noise_seed': None}

# =======================================
# IMAGE TO IMAGE
# =======================================
def image_to_image(image_prompt=None, image_path=None, lora=None, nsfw=False, skin_detailer=True,file_name="edit",save_path='outputs', project_id='default_project', workflow_override=False):
    print("IMAGE TO IMAGE GENERATION")
    print(f"Received image prompt: {image_prompt},\n Image path: {image_path}")
    # Forwards the image prompt and image to the ComfyUI service to generate an image
    service_type = 'i2i'
    #Get the workflow for this endpoint. For now we can hardcode it, but eventually we may want to allow users to specify different workflows for different endpoints.
    workflow = None
    if workflow_override is False:
        i2i_workflow_path = WORKFLOWS[service_type]
        with open(i2i_workflow_path, "r") as f:
            workflow = json.load(f)
    else:
        with open(workflow_override, "r") as f:
            workflow = json.load(f)
    
    inputNodes = "105"
    noiseNodes = "107"
    saveNodeEnhancedFull = "217"#qwen_edit/qwen_edit_full_enhanced

    """if nsfw:
        print("boost the strength of the NSFW lora")
        workflow["169"]["inputs"]["strength_model"] = 0.6
    if not skin_detailer:
        print("disabling skin detailer")
        workflow["222"]["inputs"]["strength_model"] = 0.0
        workflow["223"]["inputs"]["strength_model"] = 0.0"""

    #assign workflow variables
    #noise seed
    ns = random.randint(1, 1000000000)
    workflow[noiseNodes]["inputs"]["seed"] = ns
    #text pompt input
    if image_prompt:
        workflow[inputNodes]["inputs"]["prompt"] = image_prompt

    # Use placeholder that will be replaced with ComfyUI's assigned filename after upload
    workflow["41"]["inputs"]["image"] = "{{INPUT_IMAGE_PLACEHOLDER}}"

    # Map placeholder to file path - ComfyUIlocal.generate() will upload the file
    # and replace the placeholder with the ComfyUI-assigned filename
    input_files = {"{{INPUT_IMAGE_PLACEHOLDER}}": image_path}

    #map the desired file name to the output nodes in the workflow
    enhanced_filename = f"{project_id}_{file_name}.png"
    file_prefix = {saveNodeEnhancedFull: enhanced_filename}

    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'workflow': workflow, 'save_path': save_path, 'project_id': project_id, 'file_prefix': file_prefix, 'input_files': input_files, 'service_type': service_type} 
        )
    print(f"\n\nResponse from ComfyUI service:\n {response_data}")

    if success and 'files_saved' in response_data:
        enhanced_full_path = None
        for file_info in response_data['files_saved']:
            if file_info['file_name'] == enhanced_filename:
                enhanced_full_path = file_info['path']
                break
        
        if enhanced_full_path:
            print(f"Enhanced full image saved at: {enhanced_full_path}")
            return {'image_path':enhanced_full_path,'noise_seed': ns}

        else:
            print("Enhanced full image not found in response.")
            return  {'image_path':None,'noise_seed': ns}
    else:
        print("Image generation failed or no files saved.")
        return {'image_path':None,'noise_seed': ns}

# ========================================
# IMAGE TO VIDEO
# ========================================
def image_to_video(image_prompt=None, image_path=None, lora=None, file_suffix="i2v", save_path='outputs', project_id='default_project'):
    """Use the i2v model to generate a video based on the generated animation prompts and image sequence. The video should be created by applying the specified animations to the corresponding images in the sequence, resulting in a dynamic and engaging video that brings the narrative to life.
    """
    service_type = 'i2v'
    #Get the workflow for this endpoint. For now we can hardcode it, but eventually we may want to allow users to specify different workflows for different endpoints.
    i2i_workflow_path = WORKFLOWS[service_type]
    with open(i2i_workflow_path, "r") as f:
        workflow = json.load(f)
    
    inputNodes = "93"
    noiseNodes = "86"
    saveNodeEnhancedFull = "167"#wan22/wanI2V
    saveNode_LastFrame = "176"#wan22/wanI2V_enhanced_img
    loraNodeHigh = "147"
    loraNodeLow = "149"


    #assign workflow variables
    #noise seed
    workflow[noiseNodes]["inputs"]["noise_seed"] = random.randint(1, 1000000000)
    #text pompt input
    workflow[inputNodes]["inputs"]["text"] = image_prompt

    # Use placeholder that will be replaced with ComfyUI's assigned filename after upload
    workflow["97"]["inputs"]["image"] = "{{INPUT_IMAGE_PLACEHOLDER}}"
    #lora
    if lora and lora != 'basic':
        #lookup key
        try:
            lora_paths = WAN_LORA_FILES_MAP.get(lora)
            workflow[loraNodeHigh]["inputs"]["lora_name"] = lora_paths['high']
            workflow[loraNodeHigh]["inputs"]["strength_model"] = 0.5
            workflow[loraNodeLow]["inputs"]["lora_name"] = lora_paths['low']
            workflow[loraNodeLow]["inputs"]["strength_model"] = 0.5
        except Exception as e:
            print(f"Error setting Lora paths: {e}")

    # Map placeholder to file path - ComfyUIlocal.generate() will upload the file
    # and replace the placeholder with the ComfyUI-assigned filename
    input_files = {"{{INPUT_IMAGE_PLACEHOLDER}}": image_path}

    #map the desired file name to the output nodes in the workflow
    enhanced_filename = f"{project_id}_{file_suffix}.mp4"
    lastFrame_filename = f"{project_id}_{file_suffix}_lf.png"
    file_prefix = {saveNodeEnhancedFull: enhanced_filename,
                   saveNode_LastFrame: lastFrame_filename}

    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'workflow': workflow, 'save_path': save_path, 'project_id': project_id, 'file_prefix': file_prefix, 'input_files': input_files, 'service_type': service_type} 
        )
    print(f"\n\nResponse from ComfyUI service:\n {response_data}")
    print(f"Returning the video, last frame image, AND new clip video which is just the same video. This keeps consitent with V2V")
    results = {'video_path': None, 'last_frame_path': None, 'clip_video_path': None}
    if success and 'files_saved' in response_data:
        #loop through the returned files and find the matches
        for file_info in response_data['files_saved']:
            #CHeck for ANY file name match to our desired output names
            if file_info['file_name'] in file_prefix.values():
                if file_info['file_name'] == enhanced_filename:
                    results['video_path'] = file_info['path']
                    results['clip_video_path'] = file_info['path']
                    print(f"Video saved at: {results['video_path']}")
                elif file_info['file_name'] == lastFrame_filename:
                    results['last_frame_path'] = file_info['path']
                    print(f"Last frame image saved at: {results['last_frame_path']}")
             
        return results
    else:
        print("Video generation failed or no files saved.")
        return results
    
# =======================================
# VIDEO TO VIDEO
# =======================================
def video_to_video(video_prompt=None, video_path=None, anchor_image_path=None, lora=None, file_suffix="v2v", save_path='outputs', project_id='default_project'):
    """Use the v2v model to generate a new video based on the generated animation prompts and the original video. The new video should be created by applying the specified animations to the corresponding frames in the original video, resulting in a dynamic and engaging video that brings the narrative to life.
    """
    global WAN_LORA_FILES_MAP
    service_type = 'v2v'
    #Get the workflow for this endpoint. For now we can hardcode it, but eventually we may want to allow users to specify different workflows for different endpoints.
    i2i_workflow_path = WORKFLOWS[service_type]
    with open(i2i_workflow_path, "r") as f:
        workflow = json.load(f)
    
    input_AnchorImgNode = "111"
    input_VideoNode = "730"
    input_promptNode = "310"
    noiseNodes = "632"
    saveNode_FullVideo = "719"#svi/svi_full_extended
    saveNode_LastFrame = "647"#svi/svi_last_frame
    saveNode_NewClip = "720"#svi/svi_clip

    loraNodeHigh = "634"
    loraNodeLow = "595"

    #assign workflow variables
    #noise seed
    workflow[noiseNodes]["inputs"]["noise_seed"] = random.randint(1, 1000000000)
    #text pompt input
    workflow[input_promptNode]["inputs"]["text"] = video_prompt
    #lora
    if lora and lora != 'basic':
        #lookup key
        lora_paths = WAN_LORA_FILES_MAP.get(lora)
        print("LORA REQUESTED - but only a ejaculation lora is currently used.")
        if lora == 'ejaculation':
            print("WARNING: using ejaculation lora, which is designed to enhance ejaculation in images/videos. This may not have a significant effect on the generated video if the video does not contain clear ejaculation or if the model does not apply the lora strongly. The impact of the lora will depend on the content of the video and how the model interprets the prompt and applies the lora during generation.")
            workflow[loraNodeHigh]["inputs"]["lora_name"] = lora_paths['high']
            workflow[loraNodeHigh]["inputs"]["strength_model"] = 0.4
            workflow[loraNodeLow]["inputs"]["lora_name"] = lora_paths['low']
            workflow[loraNodeLow]["inputs"]["strength_model"] = 0.4

    # Use placeholder that will be replaced with ComfyUI's assigned filename after upload
    workflow[input_AnchorImgNode]["inputs"]["image"] = "{{INPUT_IMAGE_PLACEHOLDER}}"
    workflow[input_VideoNode]["inputs"]["video"] = "{{INPUT_VIDEO_PLACEHOLDER}}"

    # Map placeholder to file path - ComfyUIlocal.generate() will upload the file
    # and replace the placeholder with the ComfyUI-assigned filename
    input_files = {"{{INPUT_IMAGE_PLACEHOLDER}}": anchor_image_path,
                   "{{INPUT_VIDEO_PLACEHOLDER}}": video_path}

    #map the desired file name to the output nodes in the workflow
    enhanced_filename = f"{project_id}_{file_suffix}.mp4"
    lastFrame_filename = f"{project_id}_{file_suffix}_lf.png"
    newClip_filename = f"{project_id}_{file_suffix}_clip.mp4"
    file_prefix = {saveNode_FullVideo: enhanced_filename,
                   saveNode_LastFrame: lastFrame_filename,
                   saveNode_NewClip: newClip_filename}

    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'workflow': workflow, 'save_path': save_path, 'project_id': project_id, 'file_prefix': file_prefix, 'input_files': input_files, 'service_type': service_type} 
        )
    print(f"\n\nResponse from ComfyUI service:\n {response_data}")
    print(f"Returning the video, last frame image, AND new clip video")
    results = {'video_path': None, 'last_frame_path': None, 'clip_video_path': None}
    if success and 'files_saved' in response_data:
        #loop through the returned files and find the matches
        for file_info in response_data['files_saved']:
            #CHeck for ANY file name match to our desired output names
            if file_info['file_name'] in file_prefix.values():
                if file_info['file_name'] == enhanced_filename:
                    results['video_path'] = file_info['path']
                    print(f"Video saved at: {results['video_path']}")

                elif file_info['file_name'] == lastFrame_filename:
                    results['last_frame_path'] = file_info['path']
                    print(f"Last frame image saved at: {results['last_frame_path']}")

                elif file_info['file_name'] == newClip_filename:
                    results['clip_video_path'] = file_info['path']
                    print(f"New clip video saved at: {results['clip_video_path']}")
        return results
    else:
        print("Video generation failed or no files saved.")
        return results

# ========================================
# VIDEO and AUDIO LIPSYNC to VIDEO
# ========================================
def video_lipsync(video_path=None, audio_path=None, lora=None, file_name="lipsync", save_path='output', project_id='default_project'):
    print("VIDEO LIPSYNC")
    print(f"Video Path: {video_path},\n Audio Path: {audio_path},\n Save Path: {save_path},\n Project ID: {project_id}")
    
    service_type = 'video_lipsync'

    workflow_path = WORKFLOWS[service_type]
    with open(workflow_path, "r") as f:
        workflow = json.load(f)

    audioInputNode = "125"#full_track (10).wav
    videoInputNode = "228"#test_run3_scene3_clip.mp4 - VHS_LoadVideo node
    projectIdNode = "241"
    saveNodeSilentVideo = "312"#InfiniteTalk/infiniteTalk_video_silent
    saveNodeVideo = "131"#InfiniteTalk/infiniteTalk_video
    saveNodeLastFreame ="335"#InfiniteTalk/last_frame

    workflow[audioInputNode]["inputs"]["audio"] = "{{INPUT_AUDIO_PLACEHOLDER}}"
    workflow[audioInputNode]["inputs"]["audioUI"] = ""  # Clear the UI field to use the audio field
    workflow[videoInputNode]["inputs"]["video"] = "{{INPUT_VIDEO_PLACEHOLDER}}"
    workflow[projectIdNode]["inputs"]["positive_prompt"] = project_id

    # Map placeholder to file path - ComfyUIlocal.generate() will upload the file
    # and replace the placeholder with the ComfyUI-assigned filename
    input_files = {"{{INPUT_AUDIO_PLACEHOLDER}}": audio_path, 
                   "{{INPUT_VIDEO_PLACEHOLDER}}": video_path}

    #map the desired file name to the output nodes in the workflow
    silent_video_filename = f"{project_id}_silent{file_name}.mp4"
    full_video_filename = f"{project_id}_full{file_name}.mp4"
    last_frame_filename = f"{project_id}_last_frame{file_name}.png"

    file_prefix = {saveNodeSilentVideo: silent_video_filename, 
                   saveNodeVideo: full_video_filename,
                   saveNodeLastFreame: last_frame_filename}
    
    print(f"running service {service_type}, input_files: {input_files}, file_prefix: {file_prefix}")
    # Forwards the video and audio to the video lipsync service to get a lipsynced video back
    success, response_data, status_code = forward_to_service(
            service_type,
            '/generate',
            {'workflow': workflow, 'file_prefix': file_prefix, 'input_files': input_files, 'service_type': service_type,'save_path': save_path, 'project_id': project_id} 
        )
    print(f"Response from Video Lipsync service: {response_data}")
    results = {'video_path': None, 'last_frame_path': None, 'silent_video_path': None}
    if success and 'files_saved' in response_data:
        #loop through the returned files and find the matches
        for file_info in response_data['files_saved']:
            #CHeck for ANY file name match to our desired output names
            if file_info['file_name'] in file_prefix.values():
                if file_info['file_name'] == silent_video_filename:
                    print(f"silent video saved at: {file_info['path']}")
                    results['silent_video_path'] = file_info['path']
                if file_info['file_name'] == full_video_filename:
                    print(f"full video with audio saved at: {file_info['path']}")
                    results['video_path'] = file_info['path']
                if file_info['file_name'] == last_frame_filename:
                    print(f"last frame saved at: {file_info['path']}")
                    results['last_frame_path'] = file_info['path']
    else:
        print("Video lipsync service did not return expected 'files_saved' in response.")
                
    return results

# ========================================
# WORKFLOW
# ========================================
def try_repair_json(json_str):
    """Attempt to repair a truncated JSON string by finding the last complete top-level entry."""
    depth = 0
    last_complete_pos = -1
    in_string = False
    escape_next = False
    for i, char in enumerate(json_str):
        if escape_next:
            escape_next = False
            continue
        if char == '\\' and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
        if in_string:
            continue
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 1:  # completed one top-level entry
                last_complete_pos = i
    if last_complete_pos < 0:
        raise ValueError("Could not find any complete entry to repair JSON")
    repaired = json_str[:last_complete_pos + 1] + '\n}'
    return json.loads(repaired)

def save_master_json(save_path, file_name, data):
    """Saves files returned from a service to the appropriate location in the project folder structure.
    file_data can be a dict with file info or a list of such dicts. Each dict should have 'filename' and 'content' (binary data) keys.
    """
    # Save the prompt json to the base image assets folder for reference
    file_path = os.path.join(save_path, file_name)
    with open(file_path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"Saved master JSON data to: {file_path}")
    return True

def load_master_json(save_path, file_name):
    """Loads the master JSON file for a project, which contains metadata and references to all assets.
    """
    file_path = os.path.join(save_path, file_name)
    if not os.path.exists(file_path):
        print(f"Master JSON file not found at: {file_path}")
        return None
    
    with open(file_path, 'r') as f:
        data = json.load(f)
    
    print(f"Loaded master JSON data from: {file_path}")
    return data

def workflow(idea=None,img_prompt =None,base_img_path=None,project_name="test_run7",projects_folder="auto_project",sub_folder_base_image="base_images"):
    #
    #   Qwen VLM and Qwen edit in general want images that divide by 28 on length and width
    #   SDXL works best with outputs that divide by 64 or 128 - and 1MP, forcing  1280x960 for base image has worked OK.
    #   Wan 2.2 use resolutions of (1280 x 720) or (960 x 540), (640x480) with a frame count (ideally 81 frames) and 16–24 fps.
    #
    
    master_json = {}#used to keep track of all assets and metadata for project, potentially for UI in future
    master_json_file_name = "master.json"
    narrative_seed = "a sexual encounter with the woman"
    
    #Start to finish workflow.
    save_path = os.path.join(PROJECTS_ROOT, projects_folder, project_name)
    os.makedirs(save_path, exist_ok=True)


    #CREATE SAVE LOCATION FOR BASE IMAGE PROMPT AND GENERATED BASE IMAGE
    os.makedirs(os.path.join(save_path, sub_folder_base_image), exist_ok=True)
    base_image_assets_path = os.path.join(save_path, sub_folder_base_image)

    #1. Get an image prompt from the VLM based on an idea or character description
    #Generate an image with the prompt we got from the VLM
    
    #idea = "a young woman with,huge sagging D-cup natural chest, short wavy black hair blue eyes, with smoky eyeliner dark mascara and bold pink lips, sharp focus on her face direct eye contact,. wearing slightly transparent black silk slip dress"
    #img_prompt = "standing in a wood frame cabana tent with white curtains. a young woman with,huge sagging D-cup natural chest, short wavy black hair blue eyes, with smoky eyeliner dark mascara and bold pink lips, sharp focus on her face direct eye contact, wearing slightly transparent black silk slip dress covering. background sand dunes. toned waist, natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography, daylight, 8k resolution"
    
    # ----
    # Testing load Json
    # -----
    print("\n\n\nCOMMENT OUT master_json = load_master_json(save_path, master_json_file_name) when running for real.\n\n\n")
    # Load the master JSON to pickup for previous point
    #master_json = load_master_json(save_path, master_json_file_name)

    if base_img_path is None:
        #-------------
        #   Generate the Prompt for Image
        #-------------
        #none will be fully unique from AI, passing idea will influence, passing exact_prompt will be exact.
        image_prompt = make_image_prompt(idea=idea, exact_prompt=img_prompt, save_path=base_image_assets_path, project_id=project_name)
        

        #sometimes model returns everything in a {'result': {...}} wrapper, so check for that and extract if needed
        if 'result' in image_prompt:
            image_prompt = image_prompt['result']
            # If result is a JSON string, parse it
            if isinstance(image_prompt, str):
                image_prompt = json.loads(image_prompt)
    
        #check that we have the components we need for image generation before proceeding
        if not all(key in image_prompt for key in ['subject', 'clothing', 'setting', 'style']):
            print("Error: Incomplete image prompt received from VLM service")
            #TODO: Rerun the VLM X number of times
            return False
        
        print(f"Generated Image Prompt: {image_prompt['subject']}, {image_prompt['clothing']}, {image_prompt['setting']}, {image_prompt['style']}")
        image_prompt_concat = f"{image_prompt['subject']}, {image_prompt['clothing']}, {image_prompt['setting']}, {image_prompt['style']}"

        # Save the image prompt to a JSON file
        prompt_data = {
            'subject': image_prompt['subject'],
            'clothing': image_prompt['clothing'],
            'setting': image_prompt['setting'],
            'style': image_prompt['style'],
            'combined': image_prompt_concat
        }
        #Set the prompt on the master json for reference
        master_json['base_image'] = prompt_data
        save_master_json(save_path, master_json_file_name, master_json)

        # Load the master JSON to verify it was saved correctly
        loaded_master_json = load_master_json(save_path, master_json_file_name)
        if loaded_master_json is None:
            print("Error: Failed to load master JSON")
            return False

        #-------------
        #   Generate the Base Image
        #-------------
        base_img_w = 1280
        base_img_h = 960
        base_image_path = text_to_image(image_prompt_concat, base_image_assets_path, project_name, sub_folder=sub_folder_base_image, width=base_img_w, height=base_img_h)
        print(f"Image generation response: {base_image_path['image_path']}, noise seed used: {base_image_path['noise_seed']}")

        #save the generated image path to the master json
        master_json['base_image']['file_path'] = base_image_path['image_path']
        master_json['base_image']['noise_seed'] = base_image_path['noise_seed']
        save_master_json(save_path, master_json_file_name, master_json)
    else:
        print(f"Using provided base image path: {base_img_path} instead of generating a new image.")
        master_json['base_image'] = {
            'file_path': base_img_path,
            'noise_seed': "User provided image, no seed",
            'subject': None,
            'clothing': None,
            'setting': None,
            'style': None,
            'combined': None
        }
        save_master_json(save_path, master_json_file_name, master_json)


    # -------------
    #  Generate the Narrative
    # -------------
    narrative = generate_narrative(image_path=master_json['base_image']['file_path'], narrative_seed=narrative_seed, save_path=save_path,project_id=project_name)
    print(f"Narrative: {narrative}")
    # narrative should returned as a plain string, not JSON, so no need to parse it
    attempts = 3 #Difficult step, give model a few chances
    while attempts > 0:
        try:
                if 'result' in narrative:
                    narrative = narrative['result']
                    # If result is a JSON string, parse it
                    if isinstance(narrative, str):
                        narrative = json.loads(narrative)
                if narrative:
                    break
        except Exception as e:
                print(f"Error parsing narrative: {e}")
                try:
                    raw = narrative if isinstance(narrative, str) else None
                    if raw:
                        narrative = try_repair_json(raw)
                        print(f"Repaired truncated narrative JSON, recovered {len(narrative)} entries")
                        if narrative:
                            break
                except Exception as repair_err:
                    print(f"JSON repair also failed: {repair_err}")
        attempts -= 1
        
    #save the narrative to the master json
    master_json['narrative'] = narrative
    save_master_json(save_path, master_json_file_name, master_json)
    
    # ---------------
    # Animate Recursively guided by the narrative.
    # ---------------

    #{
    #"num": "scene activity",
    #...
    #"num": "scene activity",
    #}

    master_json['scenes'] = {}
    previous_video_clip = None
    full_video_path = None #This will constanly be updated with progressivly longer videos as we add each clip
    for num, scene in master_json['narrative'].items():
        #if first scene, use the base image and send the narrative for num
        #return will be a json with 3 keys 1,2,3 keys 'animation_prompt', 'anchor_prompt'
        #animate each, then review the video to make sure it matches the prompt, if not retry 3 times rating each time, if 4 fails use a scene change and convert to using previous image to start a new animation
        #continue untill all of the scens are done.
        attempts = 3 #Difficult step, give model a few chances
        if num == "1":
            print(f"Generating scene {num} using the base image as the anchor and no previous video clip")
            scene_clips = generate_scene_animation_prompts(narrative=scene, image_path=master_json['base_image']['file_path'], save_path=base_image_assets_path, project_id=project_name)
        else:
            print(f"Generating scene {num} using the previous scene's video clip")
            nextIndex = str(int(num)+1) 
            next_scene = master_json['scenes'].get(nextIndex, "NO NEXT SCENE THIS IS FINAL SCENE")
            scene_clips = generate_scene_animation_prompts(narrative=scene, video_path=previous_video_clip, next_narrative=next_scene, save_path=base_image_assets_path, project_id=project_name)    

        #STRUCTURE OF master_json['scenes']
        example = {
            "1": {
                "animation_prompt": "She turns her head slightly to the right, eyes locked with the camera, then slowly steps forward one foot at a time, sand shifting under her bare feet as the golden desert sun warms her skin.",
                "anchor_image_prompt": "Keep the person's face and identity exactly the same. She is standing, facing the camera, one foot slightly forward, bare feet in sand, crop top still on. Eye level view. Sharp crisp skin texture and hair detail. Sharp focus on her face.",
                "lora": "doggystyle"
            },
            "2": {
                "animation_prompt": "She lowers her head slightly, gaze drifting down from the camera’s eyes to her own chest, as if tracing the curve of her collarbone with her gaze, slow and deliberate.",
                "anchor_image_prompt": "Keep the person's face and identity exactly the same. She is standing, head tilted down, eyes looking at her own chest, crop top still on. Eye level view. Sharp crisp skin texture and hair detail. Sharp focus on her face.",
                "lora": "doggystyle"
            }
        }
            

        print(f"\n\nScene {num} Scripts: {scene_clips}\n")
        if 'result' in scene_clips:
            scene_clips = scene_clips['result']
            # If result is a JSON string, parse it
            if isinstance(scene_clips, str):
                try:
                    scene_clips = json.loads(scene_clips)
                except json.JSONDecodeError as e:
                    print(f"Error parsing JSON for scene {num}: {e}")
                    continue

        master_json['scenes'][num] = scene_clips
        save_master_json(save_path, master_json_file_name, master_json)
        
        #for each anchor image prompt, generate the anchor image
        for i, clips in scene_clips.items():
            passed_review = False
            while not passed_review:
                base_img_path = master_json['base_image']['file_path']
                # Verify each clip entry is a dictionary
                if not isinstance(clips, dict):
                    print(f"Error: clips for scene {num}, index {i} is not a dictionary: {type(clips)}")
                    print(f"Content: {clips}")
                    continue
                
                #ALL anchor images come from the original base image to help with consistency.
                anchor_image = image_to_image(image_prompt=clips['anchor_image_prompt'], image_path=base_img_path, save_path=base_image_assets_path, project_id=project_name, file_name=f"scene{num}_anchor{i}")
                anchor_image_path = anchor_image['image_path']
                animation_prompt = clips['animation_prompt']
                

                print(f"Generated Anchor Image for Scene {num} Anchor {i}: {anchor_image_path}")
                master_json['scenes'][num][i]['anchor_image_path'] = anchor_image_path #set the resulting anchor image path in the master json for this scene and clip
                master_json['scenes'][num][i]['noise_seed'] = anchor_image['noise_seed'] #save the noise seed for reference


                #save after each anchor image generation in case of failure and need to retry, we don't want to lose all progress
                save_master_json(save_path, master_json_file_name, master_json)

                #Generate animation
                if previous_video_clip:
                    master_json['scenes'][num][i]['video_prompt_path'] = previous_video_clip
                    lora = clips.get('lora', None)
                    #use the full video as source, so it will concat the new frame to the entire video
                    scene_result = video_to_video(video_prompt=animation_prompt, video_path=full_video_path, anchor_image_path=anchor_image_path, lora=lora,file_suffix=f"scene{num}_anchor{i}", save_path=base_image_assets_path, project_id=project_name)
                    print(f"Generated video, last frame, and new clip path: {scene_result}")
                else:
                    #first scene, use BASE IMAGE to video
                    master_json['scenes'][num][i]['image_prompt_path'] = base_img_path
                    scene_result = image_to_video(image_prompt=animation_prompt, image_path=base_img_path, file_suffix=f"scene{num}_anchor{i}", save_path=base_image_assets_path, project_id=project_name)
                    print(f"Generated video, last frame, and new clip path: {scene_result}")

                #set the previous video clip
                previous_video_clip = scene_result['clip_video_path']#use clip not full video_path to save vram and speed up next video generation, this is the main advantage of generating a separate clip video in the v2v workflow
                full_video_path = scene_result['video_path']#we can also keep track of the full video path if we want to use it for review or final output, but for now we will just use the clip for the next iteration
                #set the paths to the generated video, last frame, and anchor image in the master json for this scene and clip
                master_json['scenes'][num][i]['video_path'] = scene_result.get('video_path')
                master_json['scenes'][num][i]['clip_video_path'] = previous_video_clip
                master_json['scenes'][num][i]['last_frame_path'] = scene_result.get('last_frame_path')
                master_json['scenes'][num][i]['first_frame_path'] = anchor_image_path
                


                #save after each clip generation in case of failure and need to retry, we don't want to lose all progress
                save_master_json(save_path, master_json_file_name, master_json)

                #TODO: ADD RECURSIVE REVIEW AND RETRY LOGIC BASED ON VIDEO REVIEW TO ENSURE PROMPT IS BEING MET, IF NOT TRY TO REGENERATE THE CLIP UP TO X TIMES BEFORE MOVING ON TO THE NEXT SCENE. THIS CAN BE DONE BY GENERATING A THUMBNAIL OR SHORT CLIP OF THE GENERATED VIDEO AND PASSING IT BACK TO THE VLM WITH THE ORIGINAL PROMPT TO GET A RATING OR FEEDBACK ON HOW WELL THE VIDEO MATCHES THE PROMPT, THEN DECIDING WHETHER TO ACCEPT OR RETRY BASED ON THAT FEEDBACK.
                review = video_review(scene=scene_clips, scene_narrative=master_json['narrative'], video_path=previous_video_clip, shot_num=i, num_shots_in_scene=len(scene_clips), save_path=base_image_assets_path, project_id=project_name)
                print(f"Video review result: {review}")
                if 'result' in review:
                    review = review['result']
                    # If result is a JSON string, parse it
                    if isinstance(review, str):
                        try:
                            review = json.loads(review)
                        except json.JSONDecodeError as e:
                            print(f"Error parsing JSON for scene {num}: {e}")

                master_json['scenes'][num][i]['review'] = review
                save_master_json(save_path, master_json_file_name, master_json)

                passed_review = True #HARDCODE for now skip reivew

    #Clip Joins - clips are usually generated in pairs of 3.88 second videos. join them for a near 8 second video clip, which is then used with SFX generation
    for num, scene in master_json['scenes'].items():
        clips = []
        for i, clip_data in scene.items():
            # Skip non-dict entries like 'joined_clip_path'
            if isinstance(clip_data, dict):
                clip_video_path = clip_data.get('clip_video_path')
                if clip_video_path:
                    clips.append(clip_video_path)
        
        joined_clip_path = combine_videos(video_list=clips,output_file_name=f"scene{num}_joined_clip.mp4",projects_folder=projects_folder, project_name=project_name)
        master_json['scenes'][num]['joined_clip_path'] = joined_clip_path
        print(f"Joined clip for scene {num} saved at: {joined_clip_path}")
        save_master_json(save_path, master_json_file_name, master_json)
        clips=[] #reset clips for next scene



    
    # -------------
    # Generate Sound Effects Prompts
    # -------------
    for num, scene in master_json['scenes'].items():
        #get the joined clip path for this scene, which will be used as input video for the SFX generation along with the narrative and scene script
        joined_clip_path = scene.get('joined_clip_path')
        print(f"Generating sound effects prompts for scene {num} using the joined clip at: {joined_clip_path}")

        sfx_prompt = generate_sfx_prompt(video_path=joined_clip_path, save_path=base_image_assets_path, project_id=project_name)
        print(f"Sound Effects Prompts: {sfx_prompt}")
        if 'result' in sfx_prompt:
            sfx_prompt = sfx_prompt['result']
        
        master_json['scenes'][num]['sound_fx_prompt'] = sfx_prompt
    
        save_master_json(save_path, master_json_file_name, master_json)

    # ------------
    # Generate Sound Effects
    # -------------
    for num, scene in master_json['scenes'].items():
        sfx_prompt = scene.get('sound_fx_prompt')
        joined_clip_path = scene.get('joined_clip_path')
        print(f"Generating sound effects for scene {num} using prompt: {sfx_prompt}")
        sfx_result = generate_sfx(sfx_prompt=sfx_prompt, video_path=joined_clip_path, save_path=base_image_assets_path, project_id=project_name)
        print(f"Generated Sound Effects: {sfx_result}")
        
        master_json['scenes'][num]['sfx_wav_path'] = sfx_result['sfx_path']
        master_json['scenes'][num]['sfx_video_path'] = sfx_result['video_path']
    
        save_master_json(save_path, master_json_file_name, master_json)

    """
    # -------------
    # Dialogue with the VLM to refine and iterate on the narrative, prompts, and outputs as needed
    # -------------
    #isolate just the animation prompt
    scenes_for_dialogue = {num: scene['animation_prompt'] for num, scene in master_json['animation_script'].items()}
    dialogue = generate_dialogue(narrative=master_json['narrative'], scenes=scenes_for_dialogue, save_path=base_image_assets_path, project_id=project_name)
    print(f"Generated Dialogue: {dialogue}")
    if 'result' in dialogue:
        dialogue = dialogue['result']
        # If result is a JSON string, parse it
        if isinstance(dialogue, str):
            dialogue = json.loads(dialogue)

    master_json['dialogue'] = dialogue
    save_master_json(save_path, master_json_file_name, master_json)
    
    
    
    # -------------
    # Design Character Voice
    # -------------
    #HACK - finish and integrate make_voice_prompt()
    voice_design_instructions = "A young woman with a sexy sultry voice who speaks in a low rhaspy tone. flirtatious and playful when she speaks"
    #HACK - the AI should design the voice, hardcoded for now because of testing
    voice_file_path = design_voice(voice_design_instructions, save_path=save_path, project_id=project_name)
    print(f"Designed voice file path: {voice_file_path}")
    #save voice design to master json
    master_json['voice'] = {
        'design_instructions': voice_design_instructions,
        'reference_file': voice_file_path,
    }
    #save master json
    _ = save_master_json(save_path, master_json_file_name, master_json)
    print(f"Saved voice design to master JSON under 'voice' key")
    """

    # -------------
    # Final Combine
    # -------------
    final_video_clips = []
    for num, scene in master_json['scenes'].items():
        path = scene.get('sfx_video_path')
        final_video_clips.append(path)
        
    final_joined_clip_path = combine_videos(video_list=final_video_clips,output_file_name=f"FINAL_MOVIE.mp4",projects_folder=projects_folder, project_name=project_name)
    master_json['final'] = final_joined_clip_path
    print(f"Joined clip for FINAL saved at: {final_joined_clip_path}")
    save_master_json(save_path, master_json_file_name, master_json)
        
    return True

def workflow_recursive(video_path, base_image, project_name="test_run7",projects_folder="auto_project", sub_folder="base_images"):
    
    save_path = os.path.join(PROJECTS_ROOT, projects_folder, project_name)
    os.makedirs(save_path, exist_ok=True)
    #CREATE SAVE LOCATION FOR BASE IMAGE PROMPT AND GENERATED BASE IMAGE
    os.makedirs(os.path.join(save_path, sub_folder), exist_ok=True)
    save_path = os.path.join(save_path, sub_folder)

    #watch video
    system_prompt = """You are a pornographic XXX adult film directors assistant that reviews videos in detail and creates prompts for the next scene based on the content of the video.
    You will be given a video to watch, your task is to describe in detail what is happening in the video, including any key events and their sequence. 
    If there is sexually explicit content, please be detailed in your description of the sexual acts, positions, and interactions between the participants.
    Return a JSON with four (4) keys 
    'review': What happened in the video that was reviewed.
    'prompt': A prompt that will animate the next clip of the scene. 
    'anchor': an Image prompt description of the pose of the subjects in the next clip. This works with the prompt to help the AI generate the next clip. Anchor images edit the ORIGINAL charater image so must be specific about changes
    'lora': Low Rank Adaptor that helps guide the animation more specifically.
    """
    directon_prompt = f"""Describe what is happening in this video.
    Mention any key events and their approximate sequence (beginning, middle, end). 
    There could be sexually explicit content, if there is, please be detailed in your description of WHAT sexual acts, positions, and interactions between the participants, if any are happening.
    HARDCORE SCENES:
    Often only the shaft of a penis is visible in sexual scenes, it is usually at the side or bottom edge of the frame and is partially obscured. That is important to note and would affect the interpretation of the scene.
    Skin color Objects entering and exiting her mouth repeatedly are often Blowjob scenes.
    Her hand moving back and forth on a skin color object is usually a handjob, 
    vaginal penetration can show a penis or could be a skin color object in the vagina, this also typically has a repeated motion, 
    stripping or striptease and any other sexual activities should be described with as much detail as possible.
    NONE HARDCORE SCENES:
    If there is no sexual content describe the video per usual.
    
    Then I want you to come up with a prompt for where the movie should go next. The prompt is only for a 5 second clip so it must me limited.
    Based on the video you just reviewed

By now you should know:
- What is she wearing?
- What is the setting/location?
- What is her appearance/style?

Create ORIGINAL scene concepts with natural progression from the video you just reviewed. The video was probably a clip related to one of the folowing:
-  OPENING - What is the general opening activity? (flirting, teasing)
-  UNDRESSING - What specific clothing items is she wearing that get removed?
-  SEXUAL ACT - What is the primary sexual activity? (blowjob, etc.)
-  SEXUAL ACT - What is the secondary sexual activity? (missionary, doggystyle, cowgirl)
-  CLIMAX - Where does ejaculation occur? (face, breasts, body)
-  AFTERMATH - What happens after? (cum interaction, part ways)

CRITICAL RULES for your progression scene prompt:
- CREATE ORIGINAL CONTENT based on the video - do not copy
- State ONLY the general activity or action for the next scene
- Simple Descriptions of HOW movements are performed (raises hand, moves leg, penis enters mouth, etc.)
- NO body position adverbs: avoid "leans", "bends", "arches"
- NO emotional words: avoid "playfully", "teasingly", "with satisfaction"
- Keep to 2 motion focused brief sentences
- Use explicit anatomical language (penis, vagina, ass, breasts, nipples)
- Use 'viewer' or 'camera' for context of actions
- Positions should make sense relative to the action. These poses and sex acts go together for example
    -- She is Kneeling for BLOWJOB and ORAL SEX
    -- She is laying on back for MISSIONARY sex
    -- She is squating above him for COWGIRL sex
    -- she is on hands and knees (tabletop pose) for DOGGYSTYLE sex

    ANCHOR IMAGE SOURCE:
    The anchor image is created by taking the original image of the woman (standing with clothing waist up shot) and applything the anchor image prompt you create. 
    That means the anchor prompt adjust that image, which is why you must be specific about how to change the image. Anchor image is generated by an AI that edits images, an image to image process.
    
    ANCHOR IMAGE PURPOSE:
    Anchor images preserve character identity while showing the END STATE of the animation. They guide body position, camera angle, clothing state, and interactions.
    

    ANCHOR IMAGE RULES:
    1. ALWAYS START: "Keep the person's face and identity exactly the same."
    2. DESCRIBE END STATE: What is visible at the END of the animation
    3. MATCH CAMERA ANGLE: Use "high angle view", "low angle view", or "eye level view"
    4. STATE CLOTHING/NUDITY: If clothing removed, explicitly state "naked" and visible anatomy (breasts, nipples, vagina, ass, etc)
    5. SHOW INTERACTIONS: Include any objects or people interacting with the subject
    6. ALWAYS END: "Sharp skin texture and hair detail."
    7. INTRODUCE OBJECTS: If you want to add something new, or take something away remove or add it to the anchor, (clothing, props, accessories, different backgrounds)

    CAMERA ANGLES:
    - High angle = looking down at subject or above their eye level
    - Low angle = looking up at subject or from below their eye level
    - Eye level = straight on view

    XXX TERMINOLOGY:
    - ejaculation → scenes with cum/ejaculation visible
    - doggystyle → penetration from behind, subject on hands and knees
    - cowgirl → subject on top, straddling partner
    - missionary → subject on back, face-up penetration


    ANCHOR EXAMPLES (NON-SEXUAL):
    Standing → "Keep the person's face and identity exactly the same. Standing upright, facing camera, arms at sides. Eye level view. Sharp skin texture and hair detail."
    
    Sitting → "Keep the person's face and identity exactly the same. Seated position, looking at camera. Eye level view. Sharp skin texture and hair detail."

    ANCHOR EXAMPLES (SEXUAL):
    Undressing → "Keep the person's face and identity exactly the same. Naked, breasts and nipples visible. Eye level view. Sharp skin texture and hair detail."
    
    Kneeling → "Keep the person's face and identity exactly the same. Kneeling, looking slightly upward. High angle view. Sharp skin texture and hair detail."
    
    Oral sex → "Keep the person's face and identity exactly the same. Penis at bottom of frame entering mouth. High angle view looking down. Sharp skin texture and hair detail."
    
    Penetration → "Keep the person's face and identity exactly the same. Penis entering vagina, breasts and navel visible. Low angle view. Sharp skin texture and hair detail."

    LORA ANIMATION Low Rank Adapter (LoRA) Helpers
    ONLY choose one of these LORA's {', '.join(WAN_LORA_FILES_MAP.keys())}
    Lora is added to the prompt to help animate specific actions

    LORA USE REFERENCE
    - ejaculation  → any scene with cum, seman or ejaculation on to the woman's face, breasts, chests, body, stomach, etc or in to her mouth
    - doggystyle  → any scene with doggystyle, where woman is on hands and knees or all fours and man is behind her his penis entering vagina from behind
    - cowgirl  → any scene where woman is above man penis is sticking up in to her vagina she is stradleing him while he lays down in cowgirl sex position
    - missionary  → any scene where she is laying down, man's penis is entering vagina and having sex in a missionary style
    - rapid_action → used for high action like fight sequences car chases
    - basic  → standard lora placeholder when no special lora is needed

    Return ONLY a JSON schema:
    {{'review': '(string)', 'prompt': '(string)', 'anchor': '(string)', 'lora': '(string)'}}
    """ 
    
    
    master_json_file_name = "master.json"
    master_json = {}
    num = "1"
    
    previous_video_clip = video_path
    full_video_path = video_path
    save_master_json(save_path, master_json_file_name, master_json)
    
    while True:
        master_json[num] = {}
        # Forwards the prompt to the VLM service
        service_type = 'vlm'
        success, response_data, status_code = forward_to_service(
                service_type,
                '/generate',
                {'prompt': directon_prompt, 'system_prompt': system_prompt, 'videos': [previous_video_clip], 'save_path': save_path, 'project_id': project_name, 'prevent_model_unload': True} 
            )
        print(f"Response from VLM service: {response_data}")
        #sometimes the model returns as a dict with one key result
        if 'result' in response_data:
            response_data = response_data['result']
            # If result is a JSON string, parse it
            if isinstance(response_data, str):
                response_data = json.loads(response_data)

        #description and next prompt
        master_json[num]['video_review'] = response_data['review']
        master_json[num]['next_prompt'] = response_data['prompt'] + ". filmed in ultra slomo with slow-motion capture camera."
        master_json[num]['anchor_prompt'] = response_data['anchor']
        master_json[num]['lora'] = response_data.get('lora', 'basic') #default to basic lora if not specified
        #placeholder dialogue
        master_json[num]['dialogue'] = ""
        #placeholder sfx
        master_json[num]['sfx'] = ""
        save_master_json(save_path, master_json_file_name, master_json)
        #generate anchor image
        anchor = image_to_image(image_prompt=master_json[num]['anchor_prompt'], image_path=base_image, save_path=save_path, project_id=project_name, file_name=f"anchor_{num}")
        #ALL anchor images come from the original base image to help with consistency.
        anchor_image_path = anchor['image_path']
        master_json[num]['anchor_prompt_noise_seed'] = anchor['noise_seed']
        
        
        takes = 3#number of times to retry animation if review fails.
        take = 1
        video_prompt = master_json[num]['next_prompt']
        animation = None
        review = None
        #TODO: CANT add videos forever, at 100mb max Comfyui handles so must manage clip size growth. or don't concat here at all and just do single clip extentions OR force scenes to be 5 clips and then start new.
        while take <= takes:
            #animate the next video
            animation = video_to_video(video_prompt=video_prompt, video_path=full_video_path, anchor_image_path=anchor_image_path, lora=master_json[num]['lora'], file_suffix=f"scene{num}_take{take}", save_path=save_path, project_id=project_name)
        

            review = video_review2(video_path=animation.get('clip_video_path'),
                                   animation_prompt=video_prompt,
                                   anchor_image_prompt=master_json[num]['anchor_prompt'])
            if 'result' in review:
                review = review['result']
                # If result is a JSON string, parse it
                if isinstance(review, str):
                    review = json.loads(review)
            """{
                "reasoning":"basis and reasonining for the scores, what should be done to improve the scores via prompt changes",
                "subject_presence": 0,
                "motion_quality": 0,
                "pov_integrity": 0,
                "prompt_alignment": 0,
                "scene_alignment": 0,
                "continuity": 0,
                "sexual_content_accuracy": 0,
                "scene_change_required": False,
                "new_prompt":"a new prompt that would improve the video based on the review, only return if scene_change_required is false"
            }"""
            if not review.get('scene_change_required', False):
                print(f"Scene passed review, moving on to next scene. Review: {review}")
                break
            else:
                print(f"Scene change required based on review, updating prompt and retrying animation. Review: {review}")
                video_prompt = review.get('new_prompt', video_prompt) #if no new prompt provided, just retry with the same prompt
                take += 1


        master_json[num]['video_result_review'] = review
        master_json[num]['video_path'] = animation.get('video_path')
        master_json[num]['clip_video_path'] = animation.get('clip_video_path')
        master_json[num]['last_frame_path'] = animation.get('last_frame_path')
        master_json[num]['first_frame_path'] = anchor_image_path
        
        #last frame of video becomes anchor image for next video
        previous_video_clip = master_json[num]['clip_video_path']
        full_video_path = master_json[num]['video_path']
        save_master_json(save_path, master_json_file_name, master_json)

        num = str(int(num)+1)

    #repeat until done
    return True

# ==================
# Pre-Determined Workflow
# ==================
def workflow_sequential(base_image_prompt=None, base_img_path=None, eye_color="bright eyes", clothing_description="no outfit description", breast_description="breasts", project_name="test_default", projects_folder="auto_project", sub_folder="base_images"):
    global PROJECTS_ROOT
    # This workflow is a predetermined sequence of scenes that doesn't rely on the output of one step to determine the next. It's more linear and less flexible, but can be useful for testing and simple projects.
    #Create image via prompt, or use provided base image
    #create scene anchor images,
    # ---topless, full nude, bj, missionary, cowgirl, doggy, facial
    #4 scenes per anchor (4x7=28 scenes - 1.5 min video)
    #add sound effects to each scene
    #concatenate scenes in to final video
    master_json_file_name = "master.json"
    master_json = {}
    scene = "1"
    save_path_project = os.path.join(PROJECTS_ROOT, projects_folder, project_name)
    os.makedirs(save_path_project, exist_ok=True)
    #CREATE SAVE LOCATION FOR BASE IMAGE PROMPT AND GENERATED BASE IMAGE
    os.makedirs(os.path.join(save_path_project, sub_folder), exist_ok=True)
    save_path = os.path.join(save_path_project, sub_folder)

    save_master_json(save_path_project, master_json_file_name, master_json)

    save_path = os.path.join(PROJECTS_ROOT, projects_folder, project_name)
    os.makedirs(save_path, exist_ok=True)
    if base_image_prompt and not base_img_path:
        base_result = text_to_image(image_prompt=base_image_prompt, save_path=save_path, project_id=project_name, file_name="base_image")
        #{'image_path':None,'noise_seed': None}
        master_json['base_image_path'] = base_result['image_path']
        master_json['noise_seed'] = base_result.get('noise_seed', None)
    elif base_img_path:
        master_json['base_image_path'] = base_img_path
        master_json['noise_seed'] = None
    else:
        print("Error: Must provide either a base image prompt or a base image path.")

    #Create the Anchor Images. Hardcoded for now with specific workflows.
    ANCHOR_CONFIGS = {
        'flirting': {
            'workflow': 'services/workflows/qwen_image_edit_XLflirting.json',
            'prompter': prompt_flirting
        },
        'striptease': {
            'workflow': 'services/workflows/qwen_image_edit_XLtopless.json',
            'prompter': prompt_striptease
        },
        'full_nude': {
            'workflow': 'services/workflows/qwen_image_edit_XLnude.json',
            'prompter': prompt_dirtyDance
        },
        'bj': {
            'workflow': 'services/workflows/qwen_image_edit_XLbj.json',
            'prompter': prompt_blowjob
        },
        'missionary': {
            'workflow': 'services/workflows/qwen_image_edit_XLmissionary.json',
            'prompter': prompt_missionary
        },
        'cowgirl': {
            'workflow': 'services/workflows/qwen_image_edit_XLcowgirl.json',
            'prompter': prompt_cowgirl
        },
        'doggy': {
            'workflow': 'services/workflows/qwen_image_edit_XLdoggy.json',
            'prompter': prompt_doggystyle
        },
        'facial': {
            'workflow': 'services/workflows/qwen_image_edit_XLfacial.json',
            'prompter': prompt_facial
        }
    }
    
    # Initialize master_json anchors without functions
    if 'anchors' not in master_json:
        master_json['anchors'] = {}

    #tiny chest,large natural breasts, breasts, large sagging natural breasts
    #IF building img pass chest arg, so can keep uniform, use eye color too
    breast_types = ['tiny chest', 'large natural breasts', 'breasts', 'large sagging natural breasts']

    #
    #   Make Anchor Images
    #
    for anchor_key, anchor_config in ANCHOR_CONFIGS.items():
        workflow_path = anchor_config['workflow']
        image_prompt=None
        #Other workflows the built in prompt is universal, no need to adjust at the moment. IF breasts sizes change too much will implemnt custom for all of them
        if anchor_key == 'flirting':
            image_prompt = random.choice(["Keep the person's face and identity exactly the same and lighting identical. Pose her touching her chest with her hand. she is laughing. sharp crisp skin texture and hair detail.",
                            "Keep the person's face and identity exactly the same and lighting identical. Pose her touching her navel area with her hand. she is smiling. sharp crisp skin texture and hair detail.",
                            "Keep the person's face and identity exactly the same and lighting identical. Pose her touching her mouth with her finger. she is staring seductivly. sharp crisp skin texture and hair detail.",
                            "Keep the person's face and identity exactly the same and lighting identical. Pose her brushing her hair with her hand. she is smiling seductively. sharp crisp skin texture and hair detail.",
                            "Keep the person's face and identity exactly the same. Repose her sitting on a chair one leg elegantly crossed over the other, her thighs are visible, eye level view. Direct eye contact with viewer, sharp crisp skin texture and hair detail.sharp focus on her face."])
        if anchor_key == 'striptease':
            image_prompt = f"Keep the person's face and identity exactly the same, same location. she is facing directly at camera and her face, chest and hips are visible, ensure she is wearing {clothing_description}. sharp crisp skin texture and hair detail. zoom out to a full-body photograph view."
    
        if anchor_key == 'full_nude':
            image_prompt = f"Keep the person's face and identity exactly the same. Remove her clothing show her {breast_description} and stomach and navel, pubic area, vagina are all visible. she is facing directly at camera with her {eye_color}. sharp crisp skin texture and hair detail.professional photograph view."
        
        if anchor_key == 'bj':
            image_prompt = f"Keep the person's face and identity exactly the same and lighting identical. Pose her kneeling naked, nipples and  {breast_description} visible in front of the lower half of a naked man's body he has a large penis erection at the bottom of the frame. Change view to high angle view POV looking down at her face from above."
        
        if anchor_key == 'missionary':
            image_prompt = f"Keep the person's face and identity exactly the same and same location. Repose laying down, knees bent legs spread, her vagina naval {breast_description} are visible she is naked. front of the lower half of a man with a large penis at the bottom of the frame there is a hand from the side masturbating the penis. she is viewed from a high angle. she is looking up at camera with her {eye_color}. Change view to high angle view POV looking down at her from above."
        
        if anchor_key == 'cowgirl':
            image_prompt = f"Keep the person's face and identity exactly the same and same location. Repose straddling the lower half of a naked man's body with large penis erection at the bottom of the frame. her vagina naval {breast_description} are visible she is naked. she is viewed from a low angle. she is looking down at camera with her {eye_color}. Change view to low angle view POV looking up at her from below."
        
        if anchor_key == 'doggy':
            image_prompt = f"Keep the person's face and identity exactly the same. Repose on her hands and knees viewed from behind, her ass raised up back and shoulders visible naked. the lower half of a naked man's body with large penis erection at the bottom of the frame. she is viewed from a high angle. she is looking back over her shoulder at camera. Change view to high angle view POV looking down at her ass from above."
        
        if anchor_key == 'facial':
            image_prompt = f"Keep the person's face and identity exactly the same and lighting identical. Pose her kneeling naked, nipples and {breast_description} visible. she is in front of the lower half of a man with a large penis at the bottom of the frame there is a hand from the side masturbating the penis. Change view to high angle view POV looking down at her face from above."


        #Workflows have pre-existing prompts built in that are used if image_prompt is None.
        anchor_result = image_to_image(workflow_override=workflow_path,image_prompt=image_prompt, image_path=master_json['base_image_path'], save_path=save_path, project_id=project_name, file_name=f"anchor_{anchor_key}")
        #anchor_result schema: {'image_path':'path to file','noise_seed': 'int value'}
        
        if anchor_key not in master_json['anchors']:
            master_json['anchors'][anchor_key] = {}
        
        master_json['anchors'][anchor_key]['workflow'] = workflow_path
        master_json['anchors'][anchor_key]['image_path'] = anchor_result['image_path']
        master_json['anchors'][anchor_key]['noise_seed'] = anchor_result['noise_seed']

        print(f"Generated Anchor Image for {anchor_key}: {anchor_result['image_path']}")
        save_master_json(save_path_project, master_json_file_name, master_json)

    
    
    #   -------------
    #   Make Videos
    #   --------------
    
    master_json['scenes'] = {}
    scene_number = 1
    previous_video_clip_path = None
    print(f"Starting video generation for scenes using anchor images. Total anchors to use: {len(ANCHOR_CONFIGS)}")
    print(f"keys: {list(master_json['anchors'].keys())}")
    for anchor_key, anchor_config in ANCHOR_CONFIGS.items():
        if anchor_key not in master_json['anchors']:
            continue
        print(f"\nProcessing anchor: {anchor_key}\n")
        anchor_data = master_json['anchors'][anchor_key]
        anchor_image_path = anchor_data['image_path']
        
        # Get the prompt function for this anchor type from the anchor data
        prompt_function = anchor_config['prompter']
        
        prompt_result = prompt_function(breast_description=breast_description, clothing_description=clothing_description, eye_color=eye_color)
        system_prompt = prompt_result['system_prompt']
        video_prompt = prompt_result['user_prompt']
        
        #At the moment only a facial scene needs a lora, the general NSFW model works well for everything else
        if anchor_key == 'facial':
            lora = 'ejaculation'
        else:
            lora = 'basic'
           
        
        # Create 3 shots per scene and anchor image seed
        shots = 3
        master_json['scenes'][anchor_key] = {}
        for shot in range(1, shots + 1):
            scene_key = str(scene_number)
            take_file_suffix = f"scene{scene_number}_{anchor_key}_shot{shot}"
            #A bit confusing, but the 'video_prompt' is a prompt for the VLM to CREATE an antimation prompt.
            #now pass the image or video to VLM to generate the spcific scene prompt.
            #IF - first scene, pass image, else pass previous video clip.
            
            if previous_video_clip_path:
                print(f"Generating video PROMPT for scene {scene_number} using anchor {anchor_key} shot {shot}. Previous video path: {previous_video_clip_path}")
                response_data = video_review3(video_path=previous_video_clip_path, image_path=None, system_prompt=system_prompt, user_prompt=video_prompt, save_path='outputs', project_id='default_project', prevent_model_unload=True)
            else:
                print(f"Generating video PROMPT for scene {scene_number} using anchor {anchor_key} shot {shot}. Anchor image path: {anchor_image_path}")
                response_data = video_review3(video_path=None, image_path=anchor_image_path, system_prompt=system_prompt, user_prompt=video_prompt, save_path='outputs', project_id='default_project', prevent_model_unload=True)
            #sometimes the model returns as a dict with one key result
            if 'result' in response_data:
                response_data = response_data['result']
                # If result is a JSON string, parse it
                if isinstance(response_data, str):
                    response_data = json.loads(response_data)
            print(f"Response from video_review3 for scene {scene_number}, anchor {anchor_key}, shot {shot}:\n {response_data}\n")

            #SCHEMA: {{'reasoning': '(string)', 'prompt': '(string)', 'sound_effects': '(string)', 'dialogue': '(string)'}}
            #update our scene data
            print("ADDING slow motion modifier to the animation prompt for this shot.")
            animation_prompt = response_data['prompt'] + " Filmed in ultra slomo slow-motion capture on a slow motion camera."
            # shot 1 uses image_to_video, subsequent shots use video_to_video
            if shot == 1:
                if scene_number == 1:
                    print(f"First shot of the first scene, using the base image as the anchor image as the starting point for animation.")
                    anchor_image_path = master_json['base_image_path']

                # First shot: image to video
                animation_result = image_to_video(
                    image_prompt=animation_prompt,
                    image_path=anchor_image_path,
                    lora=lora,
                    file_suffix=take_file_suffix,
                    save_path=save_path,
                    project_id=project_name
                )
               
            else:
                # Subsequent shots: video to video
                animation_result = video_to_video(
                    video_prompt=animation_prompt,
                    video_path=previous_video_clip_path,
                    anchor_image_path=anchor_image_path,
                    lora=lora,
                    file_suffix=take_file_suffix,
                    save_path=save_path,
                    project_id=project_name
                )
            previous_video_clip_path = animation_result.get('clip_video_path')
            # Store scene data
            master_json['scenes'][anchor_key][str(shot)] = {
                'anchor_type': anchor_key,
                'anchor_image_path': anchor_image_path,
                'shot': shot,
                #'video_path': animation_result.get('video_path'), #DON"T save the video_path, it's just the previous frame concat. instead stick with clip and we combine all at the end
                'clip_video_path': previous_video_clip_path,
                'last_frame_path': animation_result.get('last_frame_path'),
                'prompt': animation_prompt,
                'lora': lora,
                'prompt_reasoning': response_data.get('reasoning', ''),
                'sound_effects': response_data.get('sound_effects', ''),
                'dialogue': unicode_replace(response_data.get('dialogue', ''))
            }
            
            
        
            save_master_json(save_path_project, master_json_file_name, master_json)

        print(f"Completed all shots for scene {scene_number} with anchor {anchor_key}. Final video path: {animation_result.get('video_path')}")
        clips = []
        for clip in master_json['scenes'][anchor_key].values():
            print(f"Add Clip video path for scene {scene_number}, anchor {anchor_key}, shot {clip.get('shot')}: {clip.get('clip_video_path')}")
            clips.append(clip.get('clip_video_path'))
        
        joined_clip_path = combine_videos(video_list=clips,output_file_name=f"scene{scene_number}_joined_clip.mp4",projects_folder=projects_folder, project_name=project_name)
        master_json['scenes'][anchor_key]['joined_clip_path'] = joined_clip_path
        print(f"Joined clip for scene {scene_number} with anchor {anchor_key} saved at: {joined_clip_path}")
        save_master_json(save_path_project, master_json_file_name, master_json)
        
        scene_number += 1
        #reset previous video path for the next scene
        previous_video_clip_path = None

    return True

if __name__ == "__main__":
    # Define available AI Services
    """
    "t2v": {
        "mode": "local",
        "type": "comfyui",
        "workflow": WORKFLOWS['image_generation']
    },
    """
    #run cleanup before starting servers to ensure no rogue processes or VRAM usage
    full_cleanup()

    #$env:CUDA_VISIBLE_DEVICES="1"
    #run with: python haughtstudio.py
    #this means ALL of the sub-servers would run on GPU 1 and it's the only GPU they see, so use gpu_index 0 for all of them since they will see GPU 1 as their GPU 0. If you want to run different sub-servers on different GPUs, you would need to launch multiple instances of haughtstudio.py with different CUDA_VISIBLE_DEVICES settings and different ports for the sub-servers in each instance.
    """"""
    servers = [{
            'name': 'Qwen3VLM',
            'module_name': 'services.qwen3VLM',
            'class_name': 'Qwen3VLM',
            'port': 6901,
            'gpu_index': 0,
            'host': '127.0.0.1',
            'services': ['vlm']
        },{
            'name': 'QwenTTS',
            'module_name': 'services.qwenTTS',
            'class_name': 'QwenTTS',
            'port': 6902,
            'gpu_index': 0,
            'host': '127.0.0.1',
            'services': ['tts']
        },{
            'name': 'ComfyUIlocal',
            'module_name': 'services.call_local_comfyui',
            'class_name': 'ComfyUIlocal',
            'port': 6903,
            'gpu_index': 0,#NOT the gpu comfyui runs on, at present no way to specify it in comfyui for different instances. this is the gpu the RELAY server to comfyui is on
            'host': '127.0.0.1',
            'services': ['i2v','t2i','i2i','v2v','video_lipsync']
        },
        {
            'name': 'SFXnsfw',
            'module_name': 'services.sfx_nsfw',
            'class_name': 'SFXnsfw',
            'port': 6904,
            'gpu_index': 0,
            'host': '127.0.0.1',
            'services': ['sfx']
        },]
    

    # Start Sub-processes
    start_model_servers(model_configs=servers)
    
    # Wait for models to load
    time.sleep(60)
    
    print("\n==========================================")
    print("AUTOMATIC HAUGHT STUDIO MASTER SERVER RUNNING")
    print("==========================================")
    print(f"Root Storage: {PROJECTS_ROOT}")
    print("Services Active:", list(SERVICES_REGISTRY.keys()))
    print("Master UI/API URL: http://0.0.0.0:6969")
    print("Press Ctrl+C to stop all servers.")
    
    breast_description = 'tiny small breasts'
    clothing_description = 'santa hat with matching red bra and panties'
    eye_color = 'hazel eyes'
    defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_FinalFull_Enhanced_00010_.png", project_name="sequential_christmas_1")

       
    try:
        #Don't run the server else it will block execution. Here for later
        #app.run(host='0.0.0.0', port=6969, debug=False)

        #run the automatic workflow
        start_time = time.time()
        #complete = workflow()
        #end_time = time.time()
        #print(f"Workflow complete: {complete}")
        #print(f"Workflow duration: {end_time - start_time} seconds")
        video_ = r"C:\Users\computer_user\Documents\code\haughtstudio\projects\auto_project\test_run21\base_images\wanI2V_00034.mp4"
        img_ = r"C:\Users\computer_user\Documents\code\haughtstudio\projects\auto_project\test_run21\base_images\image_960_00038_.png"

        
        
        breast_description = 'natural breasts'
        clothing_description = 'topless with leaf panties'
        eye_color = 'brown eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\qwen_edit_full_enhanced_01847_.png", project_name="sequential_nymph_1")

        breast_description = 'natural breasts'
        clothing_description = 'spaceship themed jumpsuit with front zipper.'
        eye_color = 'brown eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\spacesuit.png", project_name="sequential_space_2")

        breast_description = 'natural breasts'
        clothing_description = 'spaceship themed jumpsuit with front zipper.'
        eye_color = 'brown eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\spacesuit.png", project_name="sequential_space_3")


        
        breast_description = 'tiny small breasts'
        clothing_description = 'black lace bra and black panties'
        eye_color = 'blue eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\8ridget_jones.png", project_name="sequential_9")


        breast_description = 'natural breasts'
        clothing_description = 'rose colored spaghetti-strap silk slip dress'
        eye_color = 'brown eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00044_.png", project_name="sequential_10")


        breast_description = 'natural breasts'
        clothing_description = 'pink transparent crop top and dark green mini skirt'
        eye_color = 'hazel eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00900_.png", project_name="sequential_11")


        #blond hair, white tank top and jean shorts, perky natural breasts, blue eyes
        #"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\3mily_smith.png"
        breast_description = 'perky natural breasts'
        clothing_description = 'white tank top and jean shorts'
        eye_color = 'blue eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\3mily_smith.png", project_name="sequential_12")

        #brown hair, black lace bra and panties, large sagging natural breasts, brown eyes
        #"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\6abrielle_doe.png"
        breast_description = 'large sagging natural breasts'
        clothing_description = 'black lace bra and panties'
        eye_color = 'brown eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\6abrielle_doe.png", project_name="sequential_13")

        #brown hair, turquoise bikini, large natural breasts, brown eyes
        #"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00619_.png"
        breast_description = 'large natural breasts'
        clothing_description = 'turquoise bikini'
        eye_color = 'brown eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00619_.png", project_name="sequential_14")

        #short wavy black hair, pink silk slip dress, large natural breasts, bright blue eyes
        #"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00986_.png"
        breast_description = 'large natural breasts'
        clothing_description = 'pink silk slip dress'
        eye_color = 'bright blue eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00986_.png", project_name="sequential_15")

        #highlights blond hair, white silk slip dress, sagging natural breasts, hazel eyes
        #"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_01049_.png"
        breast_description = 'sagging natural breasts'
        clothing_description = 'white silk slip dress'
        eye_color = 'hazel eyes'
        defined = workflow_sequential(eye_color=eye_color, clothing_description=clothing_description, breast_description=breast_description, base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_01049_.png", project_name="sequential_16")

        infinite = workflow_recursive(video_path=video_, base_image=img_, project_name="test_run23")

        complete = workflow(base_img_path=r"C:\Users\computer_user\Documents\code\haughtstudio\projects\auto_project\test_run19\base_images\image_960_00029_.png",project_name="test_run23")
        end_time = time.time()
        print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")
        start_time = time.time()
        

        complete = workflow(idea="Sweedish woman with blue eyes and platinum blonde hair bob cut with bangs in a bikini on the beach",project_name="test_run18")
        end_time = time.time()
        print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")
        start_time = time.time()

        #complete = workflow(idea="young woman with brown eyes and platinum blonde hair ponytail, park bench",project_name="test_run16")
        end_time = time.time()
        print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")
        start_time = time.time()

        #complete = workflow(idea="MILF with green eyes and flowing auburn hair large sagging natural breasts, outside grassy meadow",project_name="test_run17")
        end_time = time.time()
        print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")
        

        """
        start_time = time.time()
        complete = workflow(idea="irish woman blue eyes frekles wavy black hair",project_name="test_run13")
        end_time = time.time()
        print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")

        start_time = time.time()
        complete = workflow(idea="older Voluptuous milf woman",project_name="test_run14")
        end_time = time.time()
        print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")

        start_time = time.time()
        complete = workflow(img_prompt="a young woman with,large natural chest shoulders back, wavy short blond hair, sexy brown eyes with black eyeliner dark mascara, large full lips, sharp focus on her face direct eye contact, wearing a rose colored silk slip dress. background a luxury tent with linen curtains. natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography, daylight, 8k resolution",project_name="test_run15")
        end_time = time.time()
        print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")
        """
        #complete = training_data_set_from_image(eye_color="hazel",character_name="0livi4_jane", save_path="0livi4",source_prompt="",source_image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_01049_.png")

        #complete = training_data_set_from_image(eye_color="blue",character_name="4mber_johnson", save_path="4mber",source_prompt="",source_image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\character\image_raw_00986_.png")
        
        #complete = training_data_set_from_image(eye_color="hazel", character_name="j3ss_long", save_path="j3ss",source_prompt="",source_image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00864_.png")
        #complete = training_data_set_from_image(character_name="4nna_smith", save_path="4nna",source_prompt="",source_image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_480_00225_.png")
        #complete = training_data_set_from_image(character_name="h4nn4h_brown", save_path="h4nn4h",source_prompt="",source_image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_480_00143_.png")
        #complete = training_data_set_from_image(character_name="ton9a_jones", save_path="ton9a",source_prompt="",source_image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00897_.png")
        #complete = training_data_set_from_image(character_name="xen4_stevens", save_path="xen4",source_prompt="",source_image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\temp_img\image_raw_00900_.png")
        
        #complete = training_data_set_from_image(eye_color="blue",breast_size="huge sagging D-cup natural chest",source_prompt="standing in a wood frame cabana tent with white curtains. a young woman with,huge sagging D-cup natural chest, short wavy black hair blue eyes, with smoky eyeliner dark mascara and bold pink lips, sharp focus on her face direct eye contact, wearing slightly transparent black silk slip dress covering. background sand dunes. toned waist, natural skin glow, natural skin texture with visible pores and light freckles, unretouched realistic skin, micro-skin pimples, faint skin wrinkles. professional photography, daylight, 8k resolution",image_path=r"C:\Users\computer_user\Documents\code\haughtstudio\character\image_raw_00986_.png")

        end_time = time.time()
        #print(f"Workflow complete: {complete}")
        print(f"Workflow duration: {end_time - start_time} seconds")
        
        # Gracefully shutdown after workflow completes
        print("\nWorkflow finished, shutting down services...")
        graceful_shutdown()
        
    except KeyboardInterrupt:
        print("\nReceived interrupt signal, shutting down...")
        graceful_shutdown()
    except Exception as e:
        print(f"\nError during execution: {e}")
        print("Attempting graceful shutdown...")
        graceful_shutdown()
