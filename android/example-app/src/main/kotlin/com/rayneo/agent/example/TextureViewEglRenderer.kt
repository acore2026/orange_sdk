package com.rayneo.agent.example

import android.content.Context
import android.graphics.SurfaceTexture
import android.util.AttributeSet
import android.view.TextureView
import org.webrtc.EglBase
import org.webrtc.EglRenderer
import org.webrtc.GlRectDrawer
import org.webrtc.RendererCommon
import org.webrtc.ThreadUtils
import org.webrtc.VideoFrame
import org.webrtc.VideoSink
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * TextureView-backed WebRTC renderer.
 *
 * WebRTC owns the render thread and draws decoded texture frames directly with EGL. The
 * TextureView only owns the Android SurfaceTexture lifecycle; no decoded frame is copied through
 * I420, JPEG, Bitmap, or the UI thread.
 */
internal class TextureViewEglRenderer @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
) : TextureView(context, attrs), TextureView.SurfaceTextureListener, VideoSink {
    private val eglRenderer = EglRenderer("TextureViewEglRenderer-${hashCode()}")
    private val layoutMeasure = RendererCommon.VideoLayoutMeasure()
    private val initialized = AtomicBoolean(false)
    private val released = AtomicBoolean(false)
    private val surfaceAttached = AtomicBoolean(false)
    private val firstFrameRendered = AtomicBoolean(false)
    private val framesReceived = AtomicLong(0)
    private val framesRendered = AtomicLong(0)

    @Volatile
    private var rendererEvents: RendererCommon.RendererEvents? = null

    @Volatile
    private var rotatedFrameWidth = 0

    @Volatile
    private var rotatedFrameHeight = 0

    @Volatile
    private var frameRotation = 0

    @Volatile
    private var lastFrameDescription = "<none>"

    @Volatile
    private var lastRenderTimeNanos = 0L

    @Volatile
    private var renderError = "<none>"

    private val renderListener = EglRenderer.RenderListener { renderTimeNanos ->
        lastRenderTimeNanos = renderTimeNanos
        framesRendered.incrementAndGet()
        if (!released.get() && firstFrameRendered.compareAndSet(false, true)) {
            rendererEvents?.onFirstFrameRendered()
        }
    }

    init {
        isOpaque = true
        surfaceTextureListener = this
    }

    fun init(
        sharedContext: EglBase.Context,
        events: RendererCommon.RendererEvents,
    ) {
        ThreadUtils.checkIsOnMainThread()
        check(initialized.compareAndSet(false, true)) { "TextureViewEglRenderer is already initialized" }
        rendererEvents = events
        eglRenderer.setErrorCallback { renderError = "GL_OUT_OF_MEMORY" }
        eglRenderer.addRenderListener(renderListener)
        eglRenderer.init(sharedContext, EglBase.CONFIG_PLAIN, GlRectDrawer())
        surfaceTexture?.takeIf { isAvailable }?.let(::attachSurface)
    }

    fun setMirror(mirror: Boolean) {
        eglRenderer.setMirror(mirror)
    }

    fun setScalingType(scalingType: RendererCommon.ScalingType) {
        layoutMeasure.setScalingType(scalingType)
        requestLayout()
    }

    fun clearImage() {
        if (!initialized.get() || released.get()) return
        firstFrameRendered.set(false)
        eglRenderer.clearImage()
    }

    fun release() {
        ThreadUtils.checkIsOnMainThread()
        if (!released.compareAndSet(false, true)) return
        rendererEvents = null
        surfaceTextureListener = null
        eglRenderer.release()
        surfaceAttached.set(false)
    }

    fun diagnosticSummary(): String {
        val received = framesReceived.get()
        val rendered = framesRendered.get()
        return "renderer=TextureView/EglRenderer frames_received=$received " +
            "frames_rendered=$rendered frames_pending_or_dropped=${(received - rendered).coerceAtLeast(0)} " +
            "surface_available=${surfaceAttached.get()} " +
            "first_frame_displayed=${firstFrameRendered.get()} last_frame=$lastFrameDescription " +
            "last_render_time_ns=$lastRenderTimeNanos render_error=$renderError"
    }

    override fun onMeasure(widthSpec: Int, heightSpec: Int) {
        val measured = layoutMeasure.measure(
            widthSpec,
            heightSpec,
            rotatedFrameWidth,
            rotatedFrameHeight,
        )
        setMeasuredDimension(measured.x, measured.y)
    }

    override fun onSizeChanged(width: Int, height: Int, oldWidth: Int, oldHeight: Int) {
        super.onSizeChanged(width, height, oldWidth, oldHeight)
        if (width > 0 && height > 0) {
            eglRenderer.setLayoutAspectRatio(width.toFloat() / height)
        }
    }

    override fun onFrame(frame: VideoFrame) {
        if (!initialized.get() || released.get()) return
        framesReceived.incrementAndGet()
        val width = frame.rotatedWidth
        val height = frame.rotatedHeight
        lastFrameDescription =
            "${frame.buffer.width}x${frame.buffer.height}@rotation=${frame.rotation}"
        if (
            width != rotatedFrameWidth ||
            height != rotatedFrameHeight ||
            frame.rotation != frameRotation
        ) {
            rotatedFrameWidth = width
            rotatedFrameHeight = height
            frameRotation = frame.rotation
            post { requestLayout() }
            rendererEvents?.onFrameResolutionChanged(
                frame.buffer.width,
                frame.buffer.height,
                frame.rotation,
            )
        }
        eglRenderer.onFrame(frame)
    }

    override fun onSurfaceTextureAvailable(surface: SurfaceTexture, width: Int, height: Int) {
        attachSurface(surface)
    }

    override fun onSurfaceTextureSizeChanged(surface: SurfaceTexture, width: Int, height: Int) = Unit

    override fun onSurfaceTextureDestroyed(surface: SurfaceTexture): Boolean {
        detachSurface()
        return true
    }

    override fun onSurfaceTextureUpdated(surface: SurfaceTexture) = Unit

    private fun attachSurface(surface: SurfaceTexture) {
        if (!initialized.get() || released.get() || !surfaceAttached.compareAndSet(false, true)) return
        eglRenderer.createEglSurface(surface)
    }

    private fun detachSurface() {
        if (!surfaceAttached.compareAndSet(true, false) || !initialized.get() || released.get()) return
        val completion = CountDownLatch(1)
        eglRenderer.releaseEglSurface(completion::countDown)
        ThreadUtils.awaitUninterruptibly(completion)
    }
}
