export function markerPlotPrice(marker, closeByDate) {
  const fillPrice = Number(marker?.price)
  if (Number.isFinite(fillPrice) && fillPrice > 0) return fillPrice
  const close = Number(closeByDate?.[marker?.date])
  return Number.isFinite(close) && close > 0 ? close : null
}
