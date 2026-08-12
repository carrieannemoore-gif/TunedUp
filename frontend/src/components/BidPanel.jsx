import React from 'react'

export default function BidPanel({ bidState, teams, onTeamBid, onEndBidding }){
  return (
    <div style={{border:'1px solid #ddd',padding:12,borderRadius:8}}>
      <div><strong>Bid-A-Note</strong></div>
      <div style={{marginTop:8}}>Current bid (seconds): <strong>{bidState.currentBid}</strong></div>
      <div style={{marginTop:8,display:'flex',gap:8}}>
        {teams.map(t => (
          <button key={t.id} onClick={()=>onTeamBid(t.id)}>{t.name} Bid</button>
        ))}
      </div>
      <div style={{marginTop:8}}>
        <button onClick={onEndBidding}>End Bidding / Play</button>
      </div>
    </div>
  )
}
