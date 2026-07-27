import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

// Credentials live in local.properties, which is gitignored: the association
// secret is what authenticates against the watch.
val localProps = Properties().apply {
    rootProject.file("local.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}

android {
    namespace = "dev.davidv.withoutings"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.davidv.withoutings"
        minSdk = 31
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        buildConfigField("String", "WATCH_MAC", "\"${localProps.getProperty("watch.mac", "")}\"")
        buildConfigField("String", "WATCH_SECRET", "\"${localProps.getProperty("watch.secret", "")}\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    kotlinOptions {
        jvmTarget = "11"
    }
    buildFeatures {
        compose = true
        buildConfig = true
    }

    sourceSets["main"].java.srcDir(layout.buildDirectory.dir("generated/uniffi"))
}

// The Rust is the app; Gradle only packages it. Both steps hang off preBuild so
// assembling cannot ship a stale .so or stale bindings.
val cargoNdk by tasks.registering(Exec::class) {
    workingDir = rootProject.projectDir
    commandLine(
        "cargo", "ndk", "-t", "arm64-v8a", "-o", "app/src/main/jniLibs",
        "build", "--release", "-p", "wpp-ffi"
    )
}

val uniffiBindings by tasks.registering(Exec::class) {
    dependsOn(cargoNdk)
    workingDir = rootProject.projectDir
    commandLine(
        "cargo", "run", "--quiet", "--release", "-p", "wpp-ffi", "--bin", "uniffi-bindgen",
        "--", "generate",
        "--library", "target/aarch64-linux-android/release/libwpp_ffi.so",
        "--language", "kotlin",
        "--out-dir", "app/build/generated/uniffi"
    )
}

tasks.named("preBuild") { dependsOn(uniffiBindings) }

dependencies {

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.ui)
    implementation(libs.androidx.ui.graphics)
    implementation(libs.androidx.ui.tooling.preview)
    implementation(libs.androidx.material3)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.jna) { artifact { type = "aar" } }
    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.ui.test.junit4)
    debugImplementation(libs.androidx.ui.tooling)
    debugImplementation(libs.androidx.ui.test.manifest)
}