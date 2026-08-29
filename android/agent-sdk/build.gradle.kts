plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.serialization")
}

android {
    namespace = "com.rayneo.agent.sdk"
    compileSdk = 34

    defaultConfig {
        minSdk = 26
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        consumerProguardFiles("consumer-rules.pro")
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    testOptions { unitTests.isReturnDefaultValues = true }
}

val masqueNativeAbis = listOf("arm64-v8a", "armeabi-v7a", "x86_64", "x86")
val verifyMasqueNativeAbis by tasks.registering {
    group = "verification"
    description = "Checks that the MASQUE JNI library is packaged for every supported ABI."
    doLast {
        val missing = masqueNativeAbis.filterNot { abi ->
            layout.projectDirectory
                .file("src/main/jniLibs/$abi/libmasque_core.so")
                .asFile
                .isFile
        }
        check(missing.isEmpty()) {
            "Missing libmasque_core.so for: ${missing.joinToString()}. " +
                "Run android/native/masque_core/build-android.sh first."
        }
    }
}

tasks.named("preBuild") { dependsOn(verifyMasqueNativeAbis) }

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    api("org.jetbrains.kotlinx:kotlinx-serialization-json:1.6.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.core:core-ktx:1.13.1")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
