plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}
android {
    namespace = "com.rayneo.demo.controller"
    compileSdk = 34
    defaultConfig {
        applicationId = "com.rayneo.demo.controller"
        minSdk = 26
        targetSdk = 34
        versionCode = 6
        versionName = "1.2.0"
    }
    buildTypes { getByName("release") { signingConfig = signingConfigs.getByName("debug") } }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
dependencies {
    implementation(project(":demo-remote"))
    implementation("androidx.core:core-ktx:1.13.1")
}
