# Voice models

The LiveKit migration uses the audio `v1-mini` turn detector bundled with
`livekit-agents` and the Silero model bundled with `livekit-plugins-silero`.
The Docker build validates that both load. No manual model download is needed.

Any ignored Smart Turn `.onnx` files left from the Agora branch are unused on
this branch. They may be retained locally for rollback; they are not copied into
the LiveKit image or mounted into its container.
