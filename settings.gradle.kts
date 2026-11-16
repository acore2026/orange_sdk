pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "agent-connect-sdk"
include(":agent-sdk")
include(":example-app")
include(":demo-remote")
include(":phone-controller")

// Keep project IDs and artifact names stable while organizing by responsibility.
project(":agent-sdk").projectDir = file("sdks/android")
project(":example-app").projectDir = file("apps/rayneo-patrol")
project(":demo-remote").projectDir = file("libs/remote-control")
project(":phone-controller").projectDir = file("apps/phone-controller")

