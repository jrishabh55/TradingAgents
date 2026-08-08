/** Plain-English tooltips for the 5-tier rating scale used by the agent core. */
const RATING_TOOLTIPS: Record<string, string> = {
  buy: 'Strong buy — build a full position',
  overweight: 'Mild buy — hold more than usual; gradually increase exposure',
  hold: 'Neutral — keep the position as-is',
  underweight: 'Mild sell — trim exposure, take partial profits',
  sell: 'Strong sell — exit the position',
}

export function ratingTooltip(rating?: string | null): string | undefined {
  return rating ? RATING_TOOLTIPS[rating.trim().toLowerCase()] : undefined
}
