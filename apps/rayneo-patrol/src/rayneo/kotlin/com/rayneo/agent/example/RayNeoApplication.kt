package com.rayneo.agent.example

import android.app.Application
import com.ffalcon.mercury.android.sdk.MercurySDK

class RayNeoApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        MercurySDK.init(this)
    }
}
