#include <jni.h>
#include <stdlib.h>
#include <string.h>

extern char *AGWOpen(char *configuration);
extern char *AGWCall(long long handle, char *command);
extern void AGWClose(long long handle);

static char *utf8_bytes(JNIEnv *env, jbyteArray value) {
    jsize size = (*env)->GetArrayLength(env, value);
    if (size > 131072) return NULL;
    char *data = calloc((size_t)size + 1, 1);
    if (data != NULL) (*env)->GetByteArrayRegion(env, value, 0, size, (jbyte *)data);
    return data;
}

static jbyteArray response_bytes(JNIEnv *env, char *result) {
    jsize size = (jsize)strlen(result);
    jbyteArray response = (*env)->NewByteArray(env, size);
    if (response != NULL) (*env)->SetByteArrayRegion(env, response, 0, size, (jbyte *)result);
    free(result);
    return response;
}

JNIEXPORT jbyteArray JNICALL
Java_com_rayneo_agent_sdk_agw_NativeAGWBridge_nativeOpen(JNIEnv *env, jobject bridge, jbyteArray configuration) {
    (void)bridge;
    char *value = utf8_bytes(env, configuration);
    if (value == NULL) return NULL;
    char *result = AGWOpen((char *)value);
    free(value);
    return response_bytes(env, result);
}

JNIEXPORT jbyteArray JNICALL
Java_com_rayneo_agent_sdk_agw_NativeAGWBridge_nativeCall(JNIEnv *env, jobject bridge, jlong handle, jbyteArray command) {
    (void)bridge;
    char *value = utf8_bytes(env, command);
    if (value == NULL) return NULL;
    char *result = AGWCall((long long)handle, (char *)value);
    free(value);
    return response_bytes(env, result);
}

JNIEXPORT void JNICALL
Java_com_rayneo_agent_sdk_agw_NativeAGWBridge_nativeClose(JNIEnv *env, jobject bridge, jlong handle) {
    (void)env; (void)bridge;
    AGWClose((long long)handle);
}
