package com.rayneo.agent.example

import org.junit.Assert.*
import org.junit.Test

class PatrolPresentationTest {
    @Test fun discoveryShowsNameAndAdvertisedCapabilitiesWithoutIdentity() {
        val state = PatrolState(phase = PatrolPhase.DISPATCHING, candidates = listOf(
            CandidateAgent("did:private:123", "Unitree Go2", setOf(AgentCapability.PATROL))))
        val view = patrolPresentation(state, true, false, false, false, false)
        assertTrue(view.title.contains("Confirm dispatch"))
        assertTrue(view.summary.contains("Unitree Go2"))
        assertTrue(view.summary.contains("Autonomous patrol"))
        assertFalse(view.summary.contains("On-site response"))
        assertFalse(view.summary.contains("did:"))
    }

    @Test fun technicalFailuresAndIdentityFallbackNeverLeakIntoSummary() {
        val state = PatrolState(candidates = listOf(CandidateAgent("did:private:123", "did:private:123")),
            failureReason = "https://10.0.0.1:9004 C-02 binding-mismatch")
        val view = patrolPresentation(state, true, false, false, false, false)
        assertEquals("Task incomplete", view.title)
        assertEquals("Patrol Robot Dog\nQuadruped robot · Field partner", view.summary)
    }

    @Test fun stalledVideoDoesNotClaimLivePatrol() {
        val view = patrolPresentation(PatrolState(phase = PatrolPhase.INSPECTING), true, true, true, true, true)
        assertEquals("Video temporarily interrupted", view.title)
    }
}
