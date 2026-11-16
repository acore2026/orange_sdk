# Video offload test asset

`video-offload-test.mp4` is a synthetic FFmpeg `testsrc2` pattern generated for
the Linux Agent A/B end-to-end test. It contains no external footage or audio.

- Codec: H.264, `yuv420p`
- Resolution: 1280 × 720
- Frame rate: 30 fps
- Duration: 8 seconds
- SHA-256: `64c87ee1202b7186f138e4639d020c5dc2e884dd8eb6c4bae6dee1dd40cad49c`

Generation command:

```bash
ffmpeg -f lavfi -i 'testsrc2=size=1280x720:rate=30:duration=8' \
  -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p \
  -movflags +faststart -an \
  -metadata title='Agent SDK End-to-End Local Video' \
  -metadata comment='Synthetic test pattern generated for the Agent SDK video offload test' \
  video-offload-test.mp4
```

Agent B loops this file until Agent A releases the computing session. Use
`--video-file` to select another local file or `--video-source camera` to test a
V4L2 source.
