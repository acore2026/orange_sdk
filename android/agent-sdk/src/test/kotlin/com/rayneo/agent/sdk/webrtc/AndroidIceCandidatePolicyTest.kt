package com.rayneo.agent.sdk.webrtc

import com.rayneo.agent.sdk.AgentSdkException
import com.rayneo.agent.sdk.ErrorCode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.webrtc.PeerConnection

class AndroidIceCandidatePolicyTest {
    @Test
    fun publishesOnlyCandidateWithC02UeAddress() {
        val wifi = "candidate:1 1 UDP 1 100.101.21.11 40000 typ host"
        val tun = "candidate:2 1 UDP 2 10.60.0.2 41000 typ host"
        val offer = "v=0\r\na=$wifi\r\na=$tun\r\na=end-of-candidates\r\n"

        val published = publishUserPlaneOffer(
            offer,
            "10.60.0.2",
            listOf(
                GatheredIceCandidate(wifi, PeerConnection.AdapterType.WIFI),
                // With the Android network monitor disabled, the TUN candidate's adapter
                // label is implementation-dependent and cannot be used as the selector.
                GatheredIceCandidate(tun, PeerConnection.AdapterType.UNKNOWN),
            ),
        )

        assertFalse(published.contains("100.101.21.11"))
        assertTrue(published.contains("a=candidate:2 1 UDP 2 10.60.0.2 41000 typ host"))
    }

    @Test
    fun acceptsExactUeCandidateRegardlessOfAdapterLabel() {
        val candidate = "candidate:3 1 UDP 3 10.60.0.2 42000 typ host"
        val offer = "v=0\r\na=$candidate\r\n"

        val published = publishUserPlaneOffer(
            offer,
            "10.60.0.2",
            listOf(GatheredIceCandidate(candidate, PeerConnection.AdapterType.ETHERNET)),
        )

        assertTrue(published.contains("10.60.0.2 42000 typ host"))
    }

    @Test
    fun rejectsOfferWithoutC02UeCandidate() {
        val wifi = "candidate:1 1 UDP 1 100.101.21.11 40000 typ host"
        val offer = "v=0\r\na=$wifi\r\n"

        val error = runCatching {
            publishUserPlaneOffer(
                offer,
                "10.60.0.2",
                listOf(GatheredIceCandidate(wifi, PeerConnection.AdapterType.WIFI)),
            )
        }.exceptionOrNull() as AgentSdkException

        assertEquals(ErrorCode.MEDIA_NEGOTIATION_FAILED, error.code)
        assertTrue(error.message.orEmpty().contains("observed adapters: WIFI=1"))
    }
}
