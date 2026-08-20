#include <jni.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

extern long long MasqueStart(int tun_fd, int udp_fd, char *server_url,
                             char *authorization, char *agent_tun_cidr,
                             char *identity_directory, int mtu);
extern int MasqueReplaceTun(long long handle, int tun_fd);
extern void MasqueStop(long long handle);

static int create_bound_socket(const char *local_ip) {
    int family = strchr(local_ip, ':') == NULL ? AF_INET : AF_INET6;
    int fd = socket(family, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP);
    if (fd < 0) return -1;
    if (family == AF_INET) {
        struct sockaddr_in address = {0};
        address.sin_family = AF_INET;
        if (inet_pton(AF_INET, local_ip, &address.sin_addr) != 1 ||
            bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
            close(fd);
            return -1;
        }
    } else {
        struct sockaddr_in6 address = {0};
        address.sin6_family = AF_INET6;
        if (inet_pton(AF_INET6, local_ip, &address.sin6_addr) != 1 ||
            bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
            close(fd);
            return -1;
        }
    }
    return fd;
}

JNIEXPORT jlong JNICALL
Java_com_rayneo_agent_sdk_masque_NativeMasqueBridge_nativeStart(
        JNIEnv *env, jobject bridge, jint tun_fd, jstring server_url,
        jstring authorization, jstring local_vlan_ip, jstring agent_tun_cidr,
        jint mtu, jstring identity_directory) {
    const char *server = (*env)->GetStringUTFChars(env, server_url, NULL);
    const char *local_ip = (*env)->GetStringUTFChars(env, local_vlan_ip, NULL);
    const char *cidr = (*env)->GetStringUTFChars(env, agent_tun_cidr, NULL);
    const char *directory = (*env)->GetStringUTFChars(env, identity_directory, NULL);
    const char *auth = authorization == NULL ? NULL :
            (*env)->GetStringUTFChars(env, authorization, NULL);
    if (server == NULL || local_ip == NULL || cidr == NULL || directory == NULL) {
        return 0;
    }

    int udp_fd = create_bound_socket(local_ip);
    jlong result = 0;
    if (udp_fd >= 0) {
        jclass bridge_class = (*env)->GetObjectClass(env, bridge);
        jmethodID protect = (*env)->GetMethodID(env, bridge_class,
                                                "protectQuicSocket", "(I)Z");
        if (protect != NULL && (*env)->CallBooleanMethod(env, bridge, protect, udp_fd)) {
            result = (jlong)MasqueStart(tun_fd, udp_fd, (char *)server,
                                        (char *)auth, (char *)cidr,
                                        (char *)directory, mtu);
            // MasqueStart always consumes udp_fd through net.FilePacketConn.
        } else {
            close(udp_fd);
        }
    }

    if (auth != NULL) (*env)->ReleaseStringUTFChars(env, authorization, auth);
    (*env)->ReleaseStringUTFChars(env, identity_directory, directory);
    (*env)->ReleaseStringUTFChars(env, agent_tun_cidr, cidr);
    (*env)->ReleaseStringUTFChars(env, local_vlan_ip, local_ip);
    (*env)->ReleaseStringUTFChars(env, server_url, server);
    return result;
}

JNIEXPORT jboolean JNICALL
Java_com_rayneo_agent_sdk_masque_NativeMasqueBridge_nativeReplaceTunFd(
        JNIEnv *env, jobject bridge, jlong handle, jint tun_fd) {
    (void)env;
    (void)bridge;
    return MasqueReplaceTun((long long)handle, tun_fd) ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT void JNICALL
Java_com_rayneo_agent_sdk_masque_NativeMasqueBridge_nativeStop(
        JNIEnv *env, jobject bridge, jlong handle) {
    (void)env;
    (void)bridge;
    MasqueStop((long long)handle);
}
