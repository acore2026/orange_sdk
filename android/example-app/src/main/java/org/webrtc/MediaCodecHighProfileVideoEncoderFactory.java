package org.webrtc;

import android.media.MediaCodecInfo;
import android.media.MediaCodecList;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Capability-based H.264 High Profile encoder factory for Android MediaCodec.
 *
 * <p>Upstream's injectable factory still gates High Profile by codec component name. Modern
 * Android devices commonly expose Codec2 component names (for example c2.qti.avc.encoder), so
 * that gate hides High Profile even when MediaCodecInfo explicitly reports AVCProfileHigh. This
 * factory uses the platform capability table, constructs the upstream hardware encoder with an
 * explicit High request, and verifies the SPS emitted by the codec before forwarding frames.</p>
 */
public final class MediaCodecHighProfileVideoEncoderFactory implements VideoEncoderFactory {
    private static final String H264_MIME_TYPE = "video/avc";
    private static final String H264_CODEC_NAME = "H264";
    private static final String ADVERTISED_PROFILE_LEVEL_ID = "64001f";
    private static final int PERIODIC_KEY_FRAME_INTERVAL_SECONDS = 3600;

    public interface EventListener {
        void onEvent(String message);
    }

    private final VideoEncoderFactory fallback;
    private final EglBase14.Context sharedContext;
    private final EventListener events;
    private final EncoderCapability highCapability;
    private final VideoCodecInfo highCodecInfo;

    public MediaCodecHighProfileVideoEncoderFactory(
            EglBase.Context eglContext,
            VideoEncoderFactory fallback,
            EventListener events) {
        this.fallback = fallback;
        this.sharedContext = eglContext instanceof EglBase14.Context
                ? (EglBase14.Context) eglContext
                : null;
        this.events = events;
        this.highCapability = findHighProfileEncoder();
        this.highCodecInfo = highCapability == null
                ? null
                : new VideoCodecInfo(H264_CODEC_NAME, highProfileParameters(), new ArrayList<>());
    }

    public boolean isHighProfileAvailable() {
        if (highCapability != null) {
            return true;
        }
        return Arrays.stream(fallback.getSupportedCodecs())
                .anyMatch(MediaCodecHighProfileVideoEncoderFactory::isH264HighProfile);
    }

    public String getHighProfileEncoderName() {
        if (highCapability != null) {
            return highCapability.codecInfo.getName();
        }
        return isHighProfileAvailable() ? "upstream-supported" : "<none>";
    }

    @Override
    public VideoEncoder createEncoder(VideoCodecInfo input) {
        if (!isH264HighProfile(input)) {
            return fallback.createEncoder(input);
        }

        VideoEncoder encoder;
        if (isAdvertisedHighProfile(input) && highCapability != null) {
            // HardwareVideoEncoder recognizes constrained-high as the signal that MediaCodec must
            // be configured with AVCProfileHigh. The RTP capability remains plain High (64001f),
            // matching the SPS emitted by the Qualcomm Codec2 encoder used by the integration B.
            encoder = new HardwareVideoEncoder(
                    new MediaCodecWrapperFactoryImpl(),
                    highCapability.codecInfo.getName(),
                    VideoCodecMimeType.H264,
                    highCapability.surfaceColorFormat,
                    highCapability.yuvColorFormat,
                    constrainedHighEncoderParameters(),
                    PERIODIC_KEY_FRAME_INTERVAL_SECONDS,
                    0,
                    new BaseBitrateAdjuster(),
                    sharedContext);
            events.onEvent(
                    "H264 High 硬件编码器已创建：" + highCapability.codecInfo.getName()
                            + "，SDP=" + ADVERTISED_PROFILE_LEVEL_ID);
        } else {
            encoder = fallback.createEncoder(input);
        }

        if (encoder == null) {
            events.onEvent("H264 High 编码器创建失败：profile=" + profileLevelId(input));
            return null;
        }
        return new SpsVerifyingEncoder(encoder, events);
    }

    @Override
    public VideoCodecInfo[] getSupportedCodecs() {
        LinkedHashSet<VideoCodecInfo> codecs = new LinkedHashSet<>();
        if (highCodecInfo != null) {
            codecs.add(highCodecInfo);
        }
        codecs.addAll(Arrays.asList(fallback.getSupportedCodecs()));
        return codecs.toArray(new VideoCodecInfo[0]);
    }

    private static EncoderCapability findHighProfileEncoder() {
        List<MediaCodecInfo> codecInfos;
        try {
            codecInfos = Arrays.asList(
                    new MediaCodecList(MediaCodecList.ALL_CODECS).getCodecInfos());
        } catch (RuntimeException error) {
            return null;
        }

        for (MediaCodecInfo codecInfo : codecInfos) {
            if (!codecInfo.isEncoder() || !MediaCodecUtils.isHardwareAccelerated(codecInfo)) {
                continue;
            }
            boolean supportsAvc = Arrays.stream(codecInfo.getSupportedTypes())
                    .anyMatch(H264_MIME_TYPE::equalsIgnoreCase);
            if (!supportsAvc) {
                continue;
            }
            try {
                MediaCodecInfo.CodecCapabilities capabilities =
                        codecInfo.getCapabilitiesForType(H264_MIME_TYPE);
                boolean supportsHigh = Arrays.stream(capabilities.profileLevels).anyMatch(level ->
                        level.profile == MediaCodecInfo.CodecProfileLevel.AVCProfileHigh
                                && level.level >= MediaCodecInfo.CodecProfileLevel.AVCLevel31);
                if (!supportsHigh) {
                    continue;
                }
                Integer surfaceColorFormat = MediaCodecUtils.selectColorFormat(
                        MediaCodecUtils.TEXTURE_COLOR_FORMATS,
                        capabilities);
                Integer yuvColorFormat = MediaCodecUtils.selectColorFormat(
                        MediaCodecUtils.ENCODER_COLOR_FORMATS,
                        capabilities);
                if (yuvColorFormat != null) {
                    return new EncoderCapability(codecInfo, surfaceColorFormat, yuvColorFormat);
                }
            } catch (IllegalArgumentException ignored) {
                // A malformed vendor capability entry must not hide later valid encoders.
            }
        }
        return null;
    }

    private static Map<String, String> highProfileParameters() {
        return Map.of(
                VideoCodecInfo.H264_FMTP_LEVEL_ASYMMETRY_ALLOWED, "1",
                VideoCodecInfo.H264_FMTP_PACKETIZATION_MODE, "1",
                VideoCodecInfo.H264_FMTP_PROFILE_LEVEL_ID, ADVERTISED_PROFILE_LEVEL_ID);
    }

    private static Map<String, String> constrainedHighEncoderParameters() {
        return Map.of(
                VideoCodecInfo.H264_FMTP_LEVEL_ASYMMETRY_ALLOWED, "1",
                VideoCodecInfo.H264_FMTP_PACKETIZATION_MODE, "1",
                VideoCodecInfo.H264_FMTP_PROFILE_LEVEL_ID,
                VideoCodecInfo.H264_CONSTRAINED_HIGH_3_1);
    }

    private static boolean isH264HighProfile(VideoCodecInfo codec) {
        if (!H264_CODEC_NAME.equalsIgnoreCase(codec.name)) {
            return false;
        }
        String profile = profileLevelId(codec);
        return profile.startsWith("64") && profile.length() == 6;
    }

    private static boolean isAdvertisedHighProfile(VideoCodecInfo codec) {
        return ADVERTISED_PROFILE_LEVEL_ID.equals(profileLevelId(codec));
    }

    private static String profileLevelId(VideoCodecInfo codec) {
        return codec.params
                .getOrDefault(VideoCodecInfo.H264_FMTP_PROFILE_LEVEL_ID, "")
                .toLowerCase(Locale.US);
    }

    /** Returns the first SPS profile-level-id from Annex-B or AVCC data, or null. */
    public static String findSpsProfileLevelId(ByteBuffer encodedData) {
        ByteBuffer data = encodedData.asReadOnlyBuffer();
        int start = data.position();
        int end = data.limit();

        for (int index = start; index + 5 < end; index++) {
            int startCodeLength = annexBStartCodeLength(data, index, end);
            if (startCodeLength == 0) {
                continue;
            }
            int nalHeader = index + startCodeLength;
            if ((unsigned(data.get(nalHeader)) & 0x1f) == 7 && nalHeader + 3 < end) {
                return profileLevelIdAt(data, nalHeader + 1);
            }
        }

        int index = start;
        while (index + 4 < end) {
            long nalLength = ((long) unsigned(data.get(index)) << 24)
                    | ((long) unsigned(data.get(index + 1)) << 16)
                    | ((long) unsigned(data.get(index + 2)) << 8)
                    | unsigned(data.get(index + 3));
            int nalHeader = index + 4;
            long nextNal = (long) nalHeader + nalLength;
            if (nalLength <= 0 || nextNal > end) {
                break;
            }
            if ((unsigned(data.get(nalHeader)) & 0x1f) == 7 && nalLength >= 4) {
                return profileLevelIdAt(data, nalHeader + 1);
            }
            index = (int) nextNal;
        }
        return null;
    }

    private static int annexBStartCodeLength(ByteBuffer data, int index, int end) {
        if (index + 3 < end
                && data.get(index) == 0
                && data.get(index + 1) == 0
                && data.get(index + 2) == 0
                && data.get(index + 3) == 1) {
            return 4;
        }
        if (index + 2 < end
                && data.get(index) == 0
                && data.get(index + 1) == 0
                && data.get(index + 2) == 1) {
            return 3;
        }
        return 0;
    }

    private static String profileLevelIdAt(ByteBuffer data, int profileIndex) {
        return String.format(
                Locale.US,
                "%02x%02x%02x",
                unsigned(data.get(profileIndex)),
                unsigned(data.get(profileIndex + 1)),
                unsigned(data.get(profileIndex + 2)));
    }

    private static int unsigned(byte value) {
        return value & 0xff;
    }

    private static final class EncoderCapability {
        final MediaCodecInfo codecInfo;
        final Integer surfaceColorFormat;
        final Integer yuvColorFormat;

        EncoderCapability(
                MediaCodecInfo codecInfo,
                Integer surfaceColorFormat,
                Integer yuvColorFormat) {
            this.codecInfo = codecInfo;
            this.surfaceColorFormat = surfaceColorFormat;
            this.yuvColorFormat = yuvColorFormat;
        }
    }

    private static final class SpsVerifyingEncoder implements VideoEncoder {
        private final VideoEncoder delegate;
        private final EventListener events;
        private final AtomicBoolean spsVerified = new AtomicBoolean(false);
        private final AtomicBoolean spsRejected = new AtomicBoolean(false);

        SpsVerifyingEncoder(VideoEncoder delegate, EventListener events) {
            this.delegate = delegate;
            this.events = events;
        }

        @Override
        public VideoCodecStatus initEncode(Settings settings, Callback callback) {
            return delegate.initEncode(settings, (encodedImage, codecSpecificInfo) -> {
                if (!spsVerified.get() && !spsRejected.get()
                        && encodedImage.frameType == EncodedImage.FrameType.VideoFrameKey) {
                    String actualProfile = findSpsProfileLevelId(encodedImage.buffer);
                    if (actualProfile != null) {
                        if (actualProfile.startsWith("64")) {
                            if (spsVerified.compareAndSet(false, true)) {
                                events.onEvent(
                                        "H264 High SPS 已校验：profile-level-id=" + actualProfile);
                            }
                        } else {
                            if (spsRejected.compareAndSet(false, true)) {
                                events.onEvent(
                                        "H264 High SPS 校验失败：实际 profile-level-id="
                                                + actualProfile);
                            }
                            return;
                        }
                    }
                }
                if (!spsRejected.get()) {
                    callback.onEncodedFrame(encodedImage, codecSpecificInfo);
                }
            });
        }

        @Override
        public VideoCodecStatus release() {
            return delegate.release();
        }

        @Override
        public VideoCodecStatus encode(VideoFrame frame, EncodeInfo info) {
            return delegate.encode(frame, info);
        }

        @Override
        public VideoCodecStatus setRateAllocation(
                BitrateAllocation allocation,
                int frameRate) {
            return delegate.setRateAllocation(allocation, frameRate);
        }

        @Override
        public VideoCodecStatus setRates(RateControlParameters parameters) {
            return delegate.setRates(parameters);
        }

        @Override
        public ScalingSettings getScalingSettings() {
            return delegate.getScalingSettings();
        }

        @Override
        public ResolutionBitrateLimits[] getResolutionBitrateLimits() {
            return delegate.getResolutionBitrateLimits();
        }

        @Override
        public String getImplementationName() {
            return delegate.getImplementationName() + "+HighSpsVerified";
        }

        @Override
        public EncoderInfo getEncoderInfo() {
            return delegate.getEncoderInfo();
        }

        @Override
        public boolean isHardwareEncoder() {
            return delegate.isHardwareEncoder();
        }
    }
}
