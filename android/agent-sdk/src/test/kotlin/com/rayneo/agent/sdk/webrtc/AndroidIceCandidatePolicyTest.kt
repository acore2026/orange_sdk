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
    fun publishesOnlyVpnCandidateAndRestoresUeAddress() {
        val wifi = "candidate:1 1 UDP 1 100.101.21.11 40000 typ host"
        val vpn = "candidate:2 1 UDP 2 device-name.local 41000 typ host"
        val offer = "v=0\r\na=$wifi\r\na=$vpn\r\na=end-of-candidates\r\n"

        val published = publishUserPlaneOffer(
            offer,
            "10.60.0.2",
            listOf(
                GatheredIceCandidate(wifi, PeerConnection.AdapterType.WIFI),
                GatheredIceCandidate(vpn, PeerConnection.AdapterType.VPN),
            ),
        )

        assertFalse(published.contains("100.101.21.11"))
        assertFalse(published.contains("device-name.local"))
        assertTrue(published.contains("a=candidate:2 1 UDP 2 10.60.0.2 41000 typ host"))
    }

    @Test
    fun acceptsExactUeCandidateWhenAdapterIsUnknown() {
        val candidate = "candidate:3 1 UDP 3 10.60.0.2 42000 typ host"
        val offer = "v=0\r\na=$candidate\r\n"

        val published = publishUserPlaneOffer(
            offer,
            "10.60.0.2",
            listOf(GatheredIceCandidate(candidate, PeerConnection.AdapterType.UNKNOWN)),
        )

        assertTrue(published.contains("10.60.0.2 42000 typ host"))
    }

    @Test
    fun rejectsOfferWithoutVpnCandidate() {
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
