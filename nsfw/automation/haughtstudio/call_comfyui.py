import json
import time
import urllib.request
import urllib.parse
import requests
import websocket
import uuid
import os
import gc
import logging
from typing import Dict, List, Optional, Union


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

#For COMFYUI, hardcode the worflows for different endpoints for now.
WORKFLOWS = {"i2i_cinematic": "comfy_workflows/qwen_image_edit_all_cinematic.json",
             "i2i_char_sheet": "comfy_workflows/character_sheet_H3_cinematic.json",
             "t2i_ideogram": "comfy_workflows/image_ideogram4_t2i_AUTO.json",
             "t2i_ideogram4_cinematic": "comfy_workflows/image_ideogram4_cinematic.json",
             "t2i_sdxl_cinematic": "comfy_workflows/Flesh4Fantasy_cinematic.json",
             "v2v": "comfy_workflows/svi_V2V_extender_hs_1280_720.json",
             "v2v_scail2": "comfy_workflows/wan21_scail2_multisubjectRef_py.json",
             "i2v": "comfy_workflows/LTX23_i2v_or_t2v_nsfw.json"}

class ComfyUIlocal():
    """ComfyUI workflow executor that conforms to the standard model interface."""
    
    # Required class attribute
    MODEL_NAME = "ComfyUI"
    
    
    def __init__(self, 
                 model_id: str = "comfyui_local",
                 comfyui_ip: str = "127.0.0.1", 
                 port: int = 8000, 
                 output_dir: str = "output",
                 cuda_device: int = 0,
                 workflow: dict = None,
                 last_workflow: str = None,
                 **kwargs):
        """
        Initialize ComfyUI client.
        
        Args:
            model_id: Identifier for this model instance
            comfyui_ip: IP address of ComfyUI server
            port: Port number of ComfyUI server
            output_dir: Directory to save outputs
            cuda_device: GPU device index (not used by ComfyUI but kept for interface compatibility)
            workflow: Workflow dictionary to execute
        """
        # Call parent constructor
        super().__init__()
        self.model_id = model_id
        self.cuda_device = cuda_device
        
        self.COMFY_SERVER = f"{comfyui_ip}:{port}"
        self.OUTPUT_DIR = output_dir
        self.client_id = str(uuid.uuid4())
        self.workflow = workflow
        self.ws = None
        self.last_workflow = last_workflow  #  variable to track the last workflow type used, for VRAM management if we use a workflow with the same base model keep it loaded, else unload it
        

        
        logging.info(f"ComfyUIlocal client initialized with Client ID: {self.client_id}")
        self.connect()
        logging.info("ComfyUIlocal WebSocket connection established.")
        self.is_loaded = True

    def connect(self):
        """Connects to the ComfyUI WebSocket"""
        self.ws = websocket.WebSocket()
        self.ws.connect(f"ws://{self.COMFY_SERVER}/ws?clientId={self.client_id}")

    def ensure_connected(self):
        """Ensures the WebSocket is connected, reconnecting if necessary"""
        try:
            if self.ws is None or not self.ws.connected:
                logging.info("WebSocket not connected, reconnecting...")
                self.connect()
                logging.info("WebSocket reconnected.")
            else:
                # Connection appears valid, but let's flush any stale messages
                self._flush_stale_messages()
        except Exception as e:
            logging.warning(f"Connection check failed ({e}), forcing reconnect...")
            self.connect()
            logging.info("WebSocket reconnected.")

    def _flush_stale_messages(self):
        """Flush any stale messages from the WebSocket before starting new work"""
        self.ws.settimeout(0.1)  # Non-blocking
        flushed_count = 0
        try:
            while True:
                try:
                    msg = self.ws.recv()
                    flushed_count += 1
                except websocket.WebSocketTimeoutException:
                    break  # No more messages
        except Exception:
            pass  # Ignore errors during flush
        finally:
            self.ws.settimeout(None)  # Reset to blocking
        if flushed_count > 0:
            logging.debug(f"Flushed {flushed_count} stale WebSocket messages")

    def upload_file(self, file_path: str, subfolder: str = "", overwrite: bool = True) -> Optional[Dict]:
        """
        Uploads a file (Image, Video, or Audio) to ComfyUI.
        ComfyUI uses the /upload/image endpoint for ALL media types.
        
        Args:
            file_path: Path to file to upload
            subfolder: Subfolder in ComfyUI to save to
            overwrite: Whether to overwrite existing files
            
        Returns:
            Response dict with filename info, or None on failure
        """
        print(f"Raw path: {repr(file_path)}")          # reveals hidden chars
        print(f"exists(): {os.path.exists(file_path)}")
        print(f"isfile(): {os.path.isfile(file_path)}")
        print(f"Uploading file to ComfyUI: {file_path}")
        print(f"Subfolder: {subfolder}, Overwrite: {overwrite}")

        if not os.path.exists(file_path):
            logging.error(f"File not found at {file_path}")
            return None

        with open(file_path, "rb") as file:
            files = {"image": file}
            data = {"overwrite": "true"}
            #audio and video files go to the /image endpoint also, it's just how comfyui puts files in the input directory
            response = requests.post(f"http://{self.COMFY_SERVER}/upload/image", files=files, data=data)
        
        if response.status_code == 200:
            result = response.json()
            logging.info(f"Uploaded {result['name']} (type: {result.get('type', 'unknown')})")
            return result
        else:
            logging.error(f"Upload failed: {response.status_code} - {response.text}")
            return None

    # Alias functions for upload files
    def upload_image(self, file_path): return self.upload_file(file_path)
    def upload_video(self, file_path): return self.upload_file(file_path)
    def upload_audio(self, file_path): return self.upload_file(file_path)

    def queue_prompt(self, workflow: Dict) -> Dict:
        """Sends the workflow to the queue"""
        p = {"prompt": workflow, "client_id": self.client_id}
        data = json.dumps(p).encode('utf-8')
        req = urllib.request.Request(f"http://{self.COMFY_SERVER}/prompt", data=data)
        return json.loads(urllib.request.urlopen(req).read())

    def track_progress(self, prompt_id: str):
        """Listens to WebSocket for completion"""
        while True:
            out = self.ws.recv()
            if isinstance(out, str):
                message = json.loads(out)
                if message['type'] == 'executing':
                    data = message['data']
                    if data['node'] is None and data['prompt_id'] == prompt_id:
                        logging.info("Execution complete.")
                        break
            else:
                continue

    def get_history(self, prompt_id: str) -> Dict:
        """Fetches the final results (filenames)"""
        with urllib.request.urlopen(f"http://{self.COMFY_SERVER}/history/{prompt_id}") as response:
            return json.loads(response.read())
    
    def download_file(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        """Downloads the generated video/image"""
        data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        query = urllib.parse.urlencode(data)
        with urllib.request.urlopen(f"http://{self.COMFY_SERVER}/view?{query}") as response:
            return response.read()

    def close(self):
        """Closes the WebSocket connection"""
        if self.ws:
            self.ws.close()

    def aggressive_cleanup(self) -> bool:
        """
        Aggressively cleans up ComfyUI's VRAM caches.
        This is the KEY to solving OOM issues with LoRA swapping.
        """
        logging.info("Performing aggressive VRAM cleanup...")
        
        try:
            # 1. Free unneeded memory
            free_response = requests.post(
                f"http://{self.COMFY_SERVER}/free",
                json={"unload_models": True, "free_memory": True}
            )
            logging.debug(f"/free endpoint: {free_response.status_code}")
            
            # 2. Clear the queue
            queue_response = requests.post(
                f"http://{self.COMFY_SERVER}/queue",
                json={"clear": True}
            )
            logging.debug(f"Queue cleared: {queue_response.status_code}")
            
            # 3. Interrupt any running execution
            interrupt_response = requests.post(
                f"http://{self.COMFY_SERVER}/interrupt"
            )
            logging.debug(f"Interrupted: {interrupt_response.status_code}")
            
            # 4. Give ComfyUI time to clean up
            time.sleep(2)
            
            # 5. Trigger Python garbage collection
            gc.collect()
            
            logging.info("Cleanup complete")
            return True
            
        except Exception as e:
            logging.warning(f"Cleanup warning (non-fatal): {e}")
            return False

    def _unload_model(self) -> bool:
        """Unload model to free VRAM (calls aggressive_cleanup)"""
        logging.info("Unloading ComfyUI models...")
        success = self.aggressive_cleanup()
        if success:
            self.is_loaded = False
        return success
    
    def _setup_model(self):
        """Setup/reload the model connection"""
        logging.info("Setting up ComfyUI connection...")
        self.ensure_connected()
        self.is_loaded = True

    def _replace_placeholder_in_workflow(self, workflow: Dict, placeholder: str, replacement: str) -> int:
        """
        Recursively search through workflow and replace all occurrences of placeholder with replacement.
        
        Args:
            workflow: The workflow dictionary to search through
            placeholder: The placeholder string to find (e.g., '{{INPUT_IMAGE_PLACEHOLDER}}')
            replacement: The value to replace the placeholder with (e.g., ComfyUI's assigned filename)
            
        Returns:
            int: Number of replacements made
        """
        replacements_made = 0
        
        def recursive_replace(obj):
            nonlocal replacements_made
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if isinstance(value, str) and value == placeholder:
                        obj[key] = replacement
                        replacements_made += 1
                        logging.info(f"Replaced placeholder '{placeholder}' with '{replacement}' at key '{key}'")
                    elif isinstance(value, (dict, list)):
                        recursive_replace(value)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    if isinstance(item, str) and item == placeholder:
                        obj[i] = replacement
                        replacements_made += 1
                        logging.info(f"Replaced placeholder '{placeholder}' with '{replacement}' at index {i}")
                    elif isinstance(item, (dict, list)):
                        recursive_replace(item)
        
        recursive_replace(workflow)
        return replacements_made

    def generate(self, 
                 workflow: Dict = None,
                 service_type: str = 'unknown_service',
                 clear_vram: bool = False,
                 file_prefix: Dict[str, Dict[str, str]] = None,
                 input_files: Dict[str, str] = None,
                 output_paths: Dict[str, str] = None) -> Dict:
        """
        Execute a ComfyUI workflow and return file data.
        
        Args:
            workflow: ComfyUI workflow dictionary (loaded from JSON)
            file_prefix: Dict mapping node_id to desired filename (e.g., {'57': 'file1.png', '43': 'file2.mp4'})
                        If not provided or node_id not in dict, uses timestamp_nodeid.ext format
            clear_vram: Whether to clear VRAM after execution
            service_type: The type of workflow being executed, e.g. 't2i', 'i2v', etc. (used for logging and VRAM management)
            input_files: Dict mapping placeholder strings to file paths.
                         The placeholder in the workflow JSON will be replaced with
                         the filename assigned by ComfyUI after upload.
                         Example:
                         {
                             '{{INPUT_IMAGE_PLACEHOLDER}}': '/path/to/base_image.png',
                             '{{REFERENCE_AUDIO_PLACEHOLDER}}': '/path/to/voice.wav'
                         }
                         In your workflow JSON, use the placeholder as the value:
                         workflow["41"]["inputs"]["image"] = "{{INPUT_IMAGE_PLACEHOLDER}}"
            output_paths: Dict mapping node_id to a local file path where the output should be saved.
                         e.g. {'67': '/path/to/save/image.png'}
                         When provided, the downloaded file is written to disk and 'saved_path' is
                         included in the result instead of raw bytes.
        Returns:
            Dictionary containing:
            {
                'type': 'files',
                'files': [
                    {
                        'filename': str,
                        'saved_path': str | None,  # set when output_paths provided
                        'mime_type': str
                    },
                    ...
                ]
            }
        """
        print("GENERATE COMFYUI LOCAL")
        print(f"Service Type: {service_type}")
        if self.last_workflow != service_type:
            print(f"\n\nWorkflow type changed from {self.last_workflow} to {service_type}, performing aggressive cleanup...\n\n")
            self.aggressive_cleanup()
            self.last_workflow = service_type

        if workflow is None:
            workflow = self.workflow
            
        print("Starting workflow execution...")
        
        #if file_prefix is None will append the node id to the filename for each output file, e.g. '1697059200_11.png', '1697059200_28.mp4', etc."
        file_ts = str(int(time.time()))

        file_results = []

        try:
            self.ensure_connected()

            # 1. Upload input files and replace placeholders in workflow
            if input_files:
                print(f"Uploading {len(input_files)} input file(s) to ComfyUI...")
                for placeholder, file_path in input_files.items():
                    print(f"Processing: {placeholder} -> {file_path}")
                    
                    # Upload the file
                    result = self.upload_file(file_path)
                    
                    if result:
                        uploaded_filename = result['name']
                        print(f"Successfully uploaded: {uploaded_filename}")
                        
                        # Replace the placeholder in the workflow with the ComfyUI-assigned filename
                        replacements = self._replace_placeholder_in_workflow(workflow, placeholder, uploaded_filename)
                        
                        if replacements == 0:
                            print(f"Warning: Placeholder '{placeholder}' not found in workflow!")
                        else:
                            print(f"Made {replacements} replacement(s) for placeholder '{placeholder}'")
                    else:
                        print(f"Error: Failed to upload: {file_path}")
                        return {'type': 'files', 'files': [], 'error': f'Failed to upload {file_path}'}

            # 2. Queue the Workflow
            response = self.queue_prompt(workflow)
            if 'prompt_id' not in response:
                error_detail = str(response)
                print(f"Error: Failed to queue prompt: {error_detail}")
                return {'type': 'files', 'files': [], 'error': f'ComfyUI rejected the workflow: {error_detail[:500]}'}
            
            prompt_id = response['prompt_id']
            print(f"Workflow queued! Prompt ID: {prompt_id}")

            # 3. Wait for completion
            self.track_progress(prompt_id)

            # 4. Get History
            history = self.get_history(prompt_id).get(prompt_id, {})
            if not history:
                print("Error: No history found for this prompt")
                return {'type': 'files', 'files': []}
            
            #print raw history for debugging
            #print(f"Workflow execution history: {json.dumps(history, indent=2)}")

            # 5. Process Outputs and prepare files for return to model_server
            if 'outputs' in history:
                for node_id, node_output in history['outputs'].items():
                    
                    # Consolidate all media outputs
                    media_items = []
                    if 'images' in node_output: media_items.extend(node_output['images'])
                    if 'gifs' in node_output:   media_items.extend(node_output['gifs'])
                    if 'videos' in node_output: media_items.extend(node_output['videos'])
                    if 'audio' in node_output:  media_items.extend(node_output['audio'])

                    for i, item in enumerate(media_items):
                        filename = item['filename']
                        subfolder = item['subfolder']
                        folder_type = item['type']
                        
                        logging.info(f"Downloading {filename}...")
                        file_data = self.download_file(filename, subfolder, folder_type)
                        
                        # Determine extension and MIME type
                        ext = os.path.splitext(filename)[1].lower()
                        
                        if ext in ['.png', '.jpg', '.jpeg', '.webp']:
                            mime_type = f"image/{ext[1:]}" if ext != '.jpg' else "image/jpeg"
                            suffix = f"img_{i+1}"
                        elif ext in ['.mp4', '.mov', '.avi', '.gif', '.webm', '.mkv']:
                            mime_type = f"video/{ext[1:]}"
                            suffix = f"vid_{i+1}"
                        elif ext in ['.wav', '.mp3', '.flac']:
                            mime_type = f"audio/{ext[1:]}"
                            suffix = f"audio_{i+1}"
                        else:
                            mime_type = "application/octet-stream"
                            suffix = f"out_{i+1}"

                        # Determine output filename based on file_prefix dict or fallback to timestamp
                        if file_prefix and isinstance(file_prefix, dict) and node_id in file_prefix:
                            print(f"\nUsing provided filename for node {node_id}: {file_prefix[node_id]}\n")
                            output_filename = file_prefix[node_id]
                        else:
                            print(f"\nNo filename provided for node {node_id}, using fallback naming convention.\n")
                            output_filename = f"{file_ts}_{node_id}{ext}"
                        
                        # Save to disk if an output path was provided for this node
                        saved_path = None
                        if output_paths and node_id in output_paths:
                            saved_path = output_paths[node_id]
                            # If saved_path is a directory or has no extension, append the output filename
                            if os.path.isdir(saved_path) or not os.path.splitext(saved_path)[1]:
                                saved_path = os.path.join(saved_path, output_filename)
                            os.makedirs(os.path.dirname(os.path.abspath(saved_path)), exist_ok=True)
                            with open(saved_path, 'wb') as out_f:
                                out_f.write(file_data)
                            logging.info(f"Saved {output_filename} to {saved_path}")

                        file_results.append({
                            'filename': output_filename,
                            'saved_path': saved_path,
                            'mime_type': mime_type
                        })
                        
                        logging.info(f"Prepared {output_filename} for return")

            if clear_vram:
                self.aggressive_cleanup()
                
            return {
                'type': 'files',
                'files': file_results
            }

        except Exception as e:
            logging.error(f"Error in workflow execution: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return {'type': 'files', 'files': [], 'error': str(e)}

    def test_inference(self) -> bool:
        """
        Run test inference with multiple workflow examples.
        Tests both text-to-image and image-to-video workflows.
        """
        logging.info("Starting test inference...")
        
        test_success = True
        
        # ========== TEST 1: Text-to-Image (SDXL) ==========
        print("\n" + "="*60)
        print("TEST 1: Text-to-Image Generation (SDXL)")
        print("="*60)
        
        t2i_workflow_path = "workflows/sdxl_lustify_endgame_docker.json"
        if os.path.exists(t2i_workflow_path):
            try:
                with open(t2i_workflow_path, "r") as f:
                    workflow = json.load(f)
                prompt = "A cyberpunk detective in the rain, 4k"
                # Update text prompt
                workflow["6"]["inputs"]["text"] = prompt
                
                print(f"Workflow: {t2i_workflow_path}")
                print(f"Prompt: {prompt}")
                print(f"Executing workflow...")
                
                result = self.generate(
                    workflow=workflow,
                    file_prefix="test_t2i",
                    clear_vram=False
                )
                
                if result['files']:
                    print(f"✓ Success! Generated {len(result['files'])} file(s):")
                    for file_info in result['files']:
                        print(f"  - {file_info['filename']} ({file_info['mime_type']}, {len(file_info['data'])} bytes)")
                else:
                    print("✗ Warning: Workflow executed but no files were generated")
                    test_success = False
                    
            except Exception as e:
                print(f"✗ Error: {e}")
                logging.error(f"Error in T2I test: {e}")
                test_success = False
        else:
            print(f"⊘ SKIPPED - Workflow not found: {t2i_workflow_path}")
        
        
        # ========== TEST : Image-to-Video ==========
        print("\n" + "="*60)
        print("TEST : Image-to-Video Generation")
        print("="*60)
        
        i2v_workflow_path = "workflows/wan22_nsfw_docker.json"
        test_image_path = "../test_data/image_480_00010_.png"
        
        if os.path.exists(i2v_workflow_path):
            if os.path.exists(test_image_path):
                try:
                    with open(i2v_workflow_path, "r") as f:
                        workflow = json.load(f)
                    
                    # Upload test image
                    print(f"Uploading input image: {test_image_path}")
                    server_filename = self.upload_file(test_image_path)
                    
                    if server_filename:
                        # Update workflow with uploaded image and prompt
                        workflow["97"]["inputs"]["image"] = server_filename["name"]
                        workflow["93"]["inputs"]["text"] = "They walk towards viewer. slow dolly out"
                        
                        print(f"Workflow: {i2v_workflow_path}")
                        print(f"Input Image: {server_filename['name']}")
                        print(f"Prompt: They walk towards viewer. slow dolly out")
                        print(f"Executing workflow...")
                        
                        result = self.generate(
                            workflow=workflow,
                            file_prefix="test_i2v",
                            clear_vram=False
                        )
                        
                        if result['files']:
                            print(f"✓ Success! Generated {len(result['files'])} file(s):")
                            for file_info in result['files']:
                                print(f"  - {file_info['filename']} ({file_info['mime_type']}, {len(file_info['data'])} bytes)")
                        else:
                            print("✗ Warning: Workflow executed but no files were generated")
                            test_success = False
                    else:
                        print(f"✗ Error: Failed to upload test image")
                        test_success = False
                        
                except Exception as e:
                    print(f"✗ Error: {e}")
                    logging.error(f"Error in I2V test: {e}")
                    test_success = False
            else:
                print(f"⊘ SKIPPED - Test image not found: {test_image_path}")
        else:
            print(f"⊘ SKIPPED - Workflow not found: {i2v_workflow_path}")
        
        
        # ========== Summary ==========
        print("\n" + "="*60)
        print("TEST SUMMARY")
        print("="*60)
        if test_success:
            print("✓ All tests completed successfully!")
        else:
            print("⚠ Some tests failed or were skipped. Check logs above.")
        print()
        
        return test_success


if __name__ == "__main__":
    ComfyUIlocal().aggressive_cleanup()
    