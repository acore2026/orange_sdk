package com.rayneo.agent.sdk.vpn

import org.junit.Assert.assertEquals
import org.junit.Test

class TunnelRouteAggregationTest {
    @Test
    fun `duplicate peer owners produce one aggregate route`() {
        val computeOnly = aggregateTunnelRoutes(
            baseRoutes = emptySet(),
            keyedPeers = mapOf("compute-control" to setOf("172.30.0.10")),
        )
        val computeAndSession = aggregateTunnelRoutes(
            baseRoutes = emptySet(),
            keyedPeers = mapOf(
                "compute-control" to setOf("172.30.0.10"),
                "offloading:session-1" to setOf("172.30.0.10"),
            ),
        )

        assertEquals(setOf("172.30.0.10/32"), computeOnly)
        assertEquals(computeOnly, computeAndSession)
    }

    @Test
    fun `aggregate routes retain base and normalize IPv6 peers`() {
        val routes = aggregateTunnelRoutes(
            baseRoutes = setOf("10.60.0.0/16"),
            keyedPeers = mapOf("g1" to setOf("2001:db8::2", "10.60.0.3")),
        )

        assertEquals(
            setOf("10.60.0.0/16", "2001:db8::2/128", "10.60.0.3/32"),
            routes,
        )
    }
}
