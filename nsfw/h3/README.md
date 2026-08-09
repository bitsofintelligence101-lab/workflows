# V2V Continuation Prompt System

A system prompt that turns a one-line brief plus reference images into a full MiniMax-H3 video-continuation prompt, with a ComfyUI workflow to run the generation.

Bring your own LLM. The system prompt is model-agnostic — anything that follows instructions reliably will do.

## Contents

```
SYSTEM_PROMPT.md              the guide, used as-is for your LLM's system prompt
workflow/                     ComfyUI workflow
examples/                     sample briefs, references, and generated prompts
```

## Use

1. Load `SYSTEM_PROMPT.md` as the system prompt in your LLM of choice.
2. Send the reference images plus a short brief as the user message. Add a one-line note for any optional image saying what it is.
3. Paste the returned prompt into the workflow's text input.

### Inputs

| Slot | Asset | Required |
| --- | --- | --- |
| 1 | Previous clip, ~20–60 frames | yes |
| 2 | Subject sheet, three views | yes |
| 3 | Empty environment plate | yes |
| 4 | Optional pose, prop, extra view, or additional subject | no |

Slot 4 accepts up to three images. Label each with a note — *"pose she ends in, shot from slightly below"*, *"prop on the table"*, *"second character who enters"* — since the note determines how it is incorporated.

### Example

Brief:

```
she walks to the sofa and sits on the arm of it, looks at camera
```

Returns a complete prompt with an alignment line, `summary`, `subject_definitions`, `integrated_multimodal_description`, `overall_soundscape`, and `non_diegetic_music`. Full input/output pairs are in `examples/`.

## Workflow

Load the JSON from `workflow/` in ComfyUI. Set the previous clip and reference images, paste the generated prompt into the text input, and run.

Chaining: feed the last frames of each output back in as slot 1 for the next pass. Re-run the system prompt each time — it restates the identity block on every generation, which is what keeps the subject from drifting across a chain.

## Notes

- One shot per generation. Continuation is continuous by design.
- Duration comes from the summed beats in the brief; the prompt states it explicitly.
- Every reference gets one job: the clip owns motion and light at the seam, the subject sheet owns identity, the room plate owns the scene but not the camera.
- If output quality drops, check that the generated prompt still has all six blocks and that no reference label is cited without a definition.

## License

<!-- add license -->