import type { BuiltinCardProps } from "./index";
import GenericToolCard from "./GenericToolCard";

/** Show the inputs inspected or submitted during a decision's ReAct loop. */
export default function DecisionToolCard(props: BuiltinCardProps) {
  return <GenericToolCard {...props} showInput />;
}
