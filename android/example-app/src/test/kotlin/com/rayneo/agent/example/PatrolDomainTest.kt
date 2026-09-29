package com.rayneo.agent.example

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PatrolDomainTest {
    @Test
    fun patrolVocabularyMapsToSafeRobotActions() {
        assertEquals(RobotAction.SCRAPE, mapRobotAction(PatrolIntent.THREATEN))
        assertEquals(RobotAction.FRONT_POUNCE, mapRobotAction(PatrolIntent.EVICT))
        assertEquals(
            PatrolActionType.FRONT_POUNCE,
            PatrolActionMapping.typeOf(
                mapPatrolAction(
                    PatrolRequest("A区域"),
                    HazardAlert("h-1", "A区域", HazardSeverity.CRITICAL, "持刀匪徒"),
                )!!,
            ),
        )
    }

    @Test
    fun candidateSelectionOnlyUsesReadyPatrolAgentsAndNearestDistance() {
        val selected = chooseCandidate(
            listOf(
                CandidateAgent("offline", "D-offline", setOf(AgentCapability.PATROL), AgentAvailability.OFFLINE, 1.0),
                CandidateAgent("far", "D-far", setOf(AgentCapability.PATROL), distanceMeters = 20.0),
                CandidateAgent("near", "D-1", setOf(AgentCapability.PATROL), distanceMeters = 2.0),
            ),
            setOf(AgentCapability.PATROL),
        )
        assertEquals("near", selected?.id)
    }

    @Test
    fun stateMachineRequiresConfirmationBeforeInspection() {
        val request = transitionPatrol(
            PatrolState(),
            PatrolEvent.ReceiveRequest(PatrolRequest("A区域")),
        ).state
        val listed = transitionPatrol(
            request,
            PatrolEvent.CandidatesDiscovered(
                listOf(CandidateAgent("dog-1", "D-1", setOf(AgentCapability.PATROL))),
            ),
        ).state
        assertEquals(PatrolPhase.DISPATCHING, listed.phase)
        assertEquals(PatrolPhase.NAVIGATING, transitionPatrol(
            listed,
            PatrolEvent.AgentSelected(listed.candidates.single()),
        ).state.phase)
        assertTrue(validatePatrolRequest(PatrolRequest("A区域")).isEmpty())
    }

    @Test
    fun spokenConfirmationPrioritizesCancellation() {
        assertEquals(true, parsePatrolConfirmation("确认派遣"))
        assertEquals(false, parsePatrolConfirmation("取消确认"))
        assertEquals(false, parsePatrolConfirmation("不要派遣"))
        assertEquals(null, parsePatrolConfirmation("机器狗在哪里"))
    }
}
