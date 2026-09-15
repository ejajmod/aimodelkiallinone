# Nowe pakiety wideo — RunPod → R2

Uruchamiaj każdą komendę osobno. Plik `clip_vision_h.safetensors` już znajduje się w R2 i nie wymaga ponownego wysyłania.

## Video Motion Control High Quality

```bash
mkdir -p /workspace/r2-upload/models/diffusion_models && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/diffusion_models/wan2.2_animate_14B_bf16.safetensors 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_animate_14B_bf16.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/loras && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/loras/wan2.2_animate_14B_relight_lora_bf16.safetensors 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_animate_14B_relight_lora_bf16.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/loras && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/loras/lightx2v_T2V_14B_cfg_step_distill_v2_lora_rank256_bf16.safetensors 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_T2V_14B_cfg_step_distill_v2_lora_rank256_bf16.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/loras && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors 'https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_low_noise.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/loras && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/loras/Wan21_PusaV1_LoRA_14B_rank512_bf16.safetensors 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Pusa/Wan21_PusaV1_LoRA_14B_rank512_bf16.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/loras && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/loras/Wan2.2-Fun-A14B-InP-low-noise-MPS.safetensors 'https://huggingface.co/alibaba-pai/Wan2.2-Fun-Reward-LoRAs/resolve/main/Wan2.2-Fun-A14B-InP-low-noise-MPS.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/vae && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/vae/Wan2_1_VAE_bf16.safetensors 'https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_bf16.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/text_encoders && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/text_encoders/umt5_xxl_fp16.safetensors 'https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp16.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/detection && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/detection/yolov10m.onnx 'https://huggingface.co/Wan-AI/Wan2.2-Animate-14B/resolve/main/process_checkpoint/det/yolov10m.onnx'
```

```bash
mkdir -p /workspace/r2-upload/models/detection && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/detection/vitpose_h_wholebody_model.onnx 'https://huggingface.co/Kijai/vitpose_comfy/resolve/main/onnx/vitpose_h_wholebody_model.onnx'
```

```bash
mkdir -p /workspace/r2-upload/models/detection && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/detection/vitpose_h_wholebody_data.bin 'https://huggingface.co/Kijai/vitpose_comfy/resolve/main/onnx/vitpose_h_wholebody_data.bin'
```

```bash
mkdir -p /workspace/r2-upload/models/sam2 && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/sam2/sam2.1_hiera_base_plus.safetensors 'https://huggingface.co/fofr/comfyui/resolve/main/sam2/sam2.1_hiera_base_plus.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/frame_interpolation && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/frame_interpolation/rife49.pth 'https://github.com/Fannovel16/ComfyUI-Frame-Interpolation/releases/download/models/rife49.pth'
```

## MiniMax H3

```bash
mkdir -p /workspace/r2-upload/models/diffusion_models && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/diffusion_models && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/text_encoders && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/vae && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/vae/minimax_h3_video_vae_fp16.safetensors 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors'
```

```bash
mkdir -p /workspace/r2-upload/models/vae && curl -fL --retry 10 --retry-delay 5 --retry-all-errors -C - -o /workspace/r2-upload/models/vae/minimax_h3_audio_vae_fp32.safetensors 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors'
```

## Jedna komenda wysyłająca wszystko do R2

Zakłada ustawione `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` i `AWS_DEFAULT_REGION=auto`.

```bash
aws s3 sync /workspace/r2-upload/models s3://aimodelki-instant-models/models --endpoint-url 'https://4a9c3801123773356c210679a596dc63.r2.cloudflarestorage.com' --only-show-errors
```
