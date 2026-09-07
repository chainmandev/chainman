import org.gradle.api.artifacts.dsl.LockMode

plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.compose)
    alias(libs.plugins.kotlin.compose)
}

group = "example"
version = "0.1.0"

kotlin {
    jvmToolchain(21)
}

val composeTarget =
    providers
        .gradleProperty("composeTarget")
        .orElse(
            providers.systemProperty("os.name").zip(providers.systemProperty("os.arch")) { os, arch ->
                val normalizedOs = os.lowercase()
                val normalizedArch = arch.lowercase()
                when {
                    normalizedOs.contains("linux") && normalizedArch in setOf("aarch64", "arm64") -> "linux-arm64"
                    normalizedOs.contains("linux") && normalizedArch in setOf("amd64", "x86_64") -> "linux-x64"
                    normalizedOs.contains("mac") && normalizedArch in setOf("aarch64", "arm64") -> "macos-arm64"
                    normalizedOs.contains("mac") && normalizedArch in setOf("amd64", "x86_64") -> "macos-x64"
                    normalizedOs.contains("windows") && normalizedArch in setOf("amd64", "x86_64") -> "windows-x64"
                    else -> error("Unsupported Compose Desktop host: $os/$arch")
                }
            },
        ).get()

dependencies {
    implementation(
        when (composeTarget) {
            "linux-arm64" -> compose.desktop.linux_arm64
            "linux-x64" -> compose.desktop.linux_x64
            "macos-arm64" -> compose.desktop.macos_arm64
            "macos-x64" -> compose.desktop.macos_x64
            "windows-x64" -> compose.desktop.windows_x64
            else -> error("Unsupported Compose Desktop target: $composeTarget")
        },
    )
    testImplementation(kotlin("test"))
}

dependencyLocking {
    lockAllConfigurations()
    lockMode = LockMode.STRICT
    lockFile = file("gradle/dependency-locks/$composeTarget.lockfile")
}

compose.desktop {
    application {
        mainClass = "example.MainKt"
    }
}

layout.buildDirectory = file("${System.getenv("TOOLCHAIN_WORK")}/compose-build")
