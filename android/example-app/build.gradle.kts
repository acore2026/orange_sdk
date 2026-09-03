plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.rayneo.agent.example"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.rayneo.agent.example"
        minSdk = 26
        targetSdk = 34
        versionCode = 9
        versionName = "0.2.7"
    }

    flavorDimensions += "deployment"
    productFlavors {
        create("generic") {
            dimension = "deployment"
            resValue("string", "app_name", "Agent Link Lab")
        }
        create("rayneo") {
            dimension = "deployment"
            applicationIdSuffix = ".rayneo"
            versionNameSuffix = "-rayneo"
            resValue("string", "app_name", "雷鸟 Agent A")
        }
    }

    buildFeatures {
        viewBinding = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation(project(":agent-sdk"))
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("com.infobip:google-webrtc:1.0.40793")
    "rayneoImplementation"(files("libs/MercuryAndroidSDK-v0.2.6.aar"))
    "rayneoImplementation"("androidx.appcompat:appcompat:1.7.0")
    "rayneoImplementation"("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    "rayneoImplementation"("androidx.lifecycle:lifecycle-viewmodel-ktx:2.8.4")
    testImplementation("junit:junit:4.13.2")
}
