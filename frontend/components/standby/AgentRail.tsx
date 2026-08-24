/**
 * The fleet: who is running, published by whom, pinned at which version, what
 * each one has actually been doing, and what it decided.
 *
 * Pinning is not devops trivia on this screen. A NIOSH investigation has to
 * reconstruct which code produced a fact two years ago, so the version a
 * department is pinned to is a fact an officer can read here.
 *
 * This module is now a thin entry point. The panel itself lives in
 * `components/fleet/`, because "the fleet" grew from a rail of identical rows
 * into a column of cards -- each with its own visual and its own reasoning
 * terminal -- and a rail of identical rows told an officer nothing about which
 * agent was doing what.
 *
 * One panel shows one loop, and the loop defaults to the slow one. In standby
 * the incident agents are not idle, they are not running at all, and listing
 * them would claim a readiness state this system does not have. When a fire
 * starts the console renders two of these instead: the incident loop beside
 * the structure, and the slow loop in a column of its own on the right, still
 * in full cards. The slow loop does not stop when a fire starts, and a fleet
 * that vanished -- or shrank to a row of chips -- would say it had.
 */

import { FleetPanel, type FleetPanelProps } from '@/components/fleet/FleetPanel';

export type { AgentActivity } from '@/components/fleet/AgentCard';

export type AgentRailProps = FleetPanelProps;

export function AgentRail(props: AgentRailProps) {
  return <FleetPanel {...props} />;
}
