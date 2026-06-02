
#Prompts used with qwen image edit
def i2i_prompts():
    nsfw_variation = {
        #half body nude
        "1": {
            "pose_prompt": "Keep the person's face and identity exactly the same. Remove her clothing she is naked show her nipples, breasts and stomach and navel, visible. she is facing directly at camera. sharp crisp skin texture and hair detail. close half-body shot professional photograph high resolution.",
            "category": "nsfw_variation",
            "position":"nude",
            "image_count": 1
            },
        #missionary
        "2": {
            "pose_prompt": "Keep the person's face and identity exactly the same. Repose laying down, knees bent legs spread, her vagina naval chest are visible. front of the lower half of a man with a large penis at the bottom of the frame the penis is about to enter the vagina. she is viewed from a high angle. she is looking up at camera. Change view to high angle view POV looking down at her from above. exact same background",
            "category": "nsfw_variation",
            "position":"missionary",
            "image_count": 1
            },
        #cowgirl
        "3": {
            "pose_prompt": "Keep the person's face and identity exactly the same. Repose them on top of straddling the lower half of a naked man's body that has large penis erection at the bottom of the frame going in to the subject woman's vagina. Show the subjecets vagina, naval, chest are visible. she is viewed from a low angle. she is looking down at camera. Change view to low angle view POV looking up at her from below.exact same background",
            "category": "nsfw_variation",
            "position":"cowgirl",
            "image_count": 1
            },
        #doggy
        "4": {
            "pose_prompt": "Keep the person's face and identity exactly the same. Repose on her hands and knees viewed from behind, her ass raised up back and shoulders visible. the lower half of a naked man's body with large penis errection at the bottom of the frame entering vagina doggystyle. she is viewed from a high angle. she is looking back over her shoulder at camera. Change view to high angle view POV looking down at her ass from behind. same background",
            "category": "nsfw_variation",
            "position":"doggystyle",
            "image_count": 1
            },
        #BJ
        "5": {
            "pose_prompt": "Keep the person's face and identity exactly the same and lighting identical. Pose her kneeling chest visible, in front of the lower half of a naked man's body a penis glans in her mouth, blowjob.her hands at base of penis shaft. Change view to high angle view POV looking down at her face from above. same background",
            "category": "nsfw_variation",
            "position":"blowjob",
            "image_count": 1
            },
        #full body nude
        "6": {
            "pose_prompt": "Keep the person's face and identity exactly the same. Remove her clothing show her nipples, breasts and stomach and navel, hips, vagina are all visible. she is facing directly at camera. sharp crisp skin texture and hair detail. full-body shot professional photograph high resolution.",
            "category": "nsfw_variation",
            "image_count": 1
            },
        
    }
    return nsfw_variation


#Prompts used with ltx2.3
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