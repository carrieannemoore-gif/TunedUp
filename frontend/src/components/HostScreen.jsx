import React, { useEffect, useState, useRef } from 'react'
import YouTubePlayer from './YouTubePlayer'
import AnswerModal from './AnswerModal'
import BidPanel from './BidPanel'
import GoldenMedleyTimer from './GoldenMedleyTimer'
import { beep } from '../utils/audio'

const DEFAULT_TEAMS = [
  { id: 1, name: 'Team 1', score: 0 },
  { id: 2, name: 'Team 2', score: 0 },
  { id: 3, name: 'Team 3', score: 0 }
]

export default function HostScreen(){
  const [teams, setTeams] = useState(DEFAULT_TEAMS)
  const [songs, setSongs] = useState([])
  const [currentIndex, setCurrentIndex] = useState(0)
  const [buzzLocked, setBuzzLocked] = useState(false)
  const [currentBuzzer, setCurrentBuzzer] = useState(null)
  const [phase, setPhase] = useState(1) // 1 = standard, 2 = bid, 3 = golden medley
  const [pointsSchedule, setPointsSchedule] = useState([10,20,30])
  const [answerModalOpen, setAnswerModalOpen] = useState(false)
  const [strictMode, setStrictMode] = useState(true)
  const [bidState, setBidState] = useState(null) // {currentBid:7, highestBidTeamId:null, biddingTurnIndex:0, bidders: [teamIds...]} for phase 2
  const [goldenState, setGoldenState] = useState(null) // {secondsLeft:30, currentSongIdx, correctCount}
  const playerRef = useRef(null)

  useEffect(()=>{
    fetch('/api/songs')
      .then(r=>r.json())
      .then(setSongs)
      .catch(err=>{
        console.warn('Could not load /api/songs, falling back to sample file');
        fetch('/sample_songs.json').then(r=>r.json()).then(setSongs)
      })
  },[])

  // Editable teams
  function updateTeamName(id, name){
    setTeams(ts => ts.map(t => t.id===id? {...t, name}: t))
  }

  // buzzer
  function onBuzz(teamId){
    if(buzzLocked) return
    setCurrentBuzzer(teamId)
    setBuzzLocked(true)
    beep(1000, 0.12)
    if(playerRef.current) playerRef.current.pause()
    // open answer modal for host to collect answer
    setAnswerModalOpen(true)
  }

  // Accept or reject answer from modal
  function handleAnswerSubmit({ title, artist }, correct=false){
    const currentSong = songs[currentIndex]
    if(!currentSong) return

    const matched = checkAnswer(title, artist, currentSong, strictMode)
    const points = determinePointsForCurrent() // simple fixed for MVP

    if(matched && correct){
      setTeams(ts => ts.map(t => t.id===currentBuzzer ? {...t, score: t.score + points} : t))
      beep(1200, 0.16)
    } else if(!matched && correct){
      // host marked correct even though fuzzy didn't match: still award
      setTeams(ts => ts.map(t => t.id===currentBuzzer ? {...t, score: t.score + points} : t))
      beep(1200, 0.16)
    } else {
      // wrong answer: penalize
      setTeams(ts => ts.map(t => t.id===currentBuzzer ? {...t, score: t.score - points} : t))
      beep(300, 0.18)
    }

    // close modal and resume
    setAnswerModalOpen(false)
    setCurrentBuzzer(null)
    setBuzzLocked(false)
    if(playerRef.current) playerRef.current.play()
  }

  // Host accept/reject quick buttons (keeps backward compatibility)
  function acceptAnswerQuick(points=10){
    if(!currentBuzzer) return
    setTeams(ts => ts.map(t => t.id===currentBuzzer ? {...t, score: t.score + points} : t))
    beep(1200, 0.16)
    setCurrentBuzzer(null)
    setBuzzLocked(false)
    if(playerRef.current) playerRef.current.play()
  }
  function rejectAnswerQuick(points=10){
    if(!currentBuzzer) return
    setTeams(ts => ts.map(t => t.id===currentBuzzer ? {...t, score: t.score - points} : t))
    beep(300, 0.18)
    setCurrentBuzzer(null)
    setBuzzLocked(false)
    if(playerRef.current) playerRef.current.play()
  }

  function nextSong(){
    setCurrentIndex(i => Math.min(i+1, songs.length-1))
    setCurrentBuzzer(null)
    setBuzzLocked(false)
  }

  function determinePointsForCurrent(){
    // simple escalating points based on index within a group of three for MVP
    const idx = currentIndex % pointsSchedule.length
    return pointsSchedule[idx]
  }

  // Phase 2: bidding
  function startBidding(){
    const bidderIds = teams.map(t => t.id)
    setBidState({ currentBid: 7, highestBidTeamId: null, biddingTurnIndex: 0, bidders: bidderIds })
    setPhase(2)
    beep(800, 0.12)
  }

  function teamPlaceBid(teamId){
    // Only allow if it's their turn and bid > 1
    if(!bidState) return
    const turnTeamId = bidState.bidders[bidState.biddingTurnIndex % bidState.bidders.length]
    if(turnTeamId !== teamId) return
    if(bidState.currentBid <= 1) return
    setBidState(bs => ({ ...bs, currentBid: bs.currentBid - 1, highestBidTeamId: teamId, biddingTurnIndex: bs.biddingTurnIndex + 1 }))
    beep(900, 0.08)
  }

  function endBiddingAndPlay(){
    // winning team is highestBidTeamId
    if(!bidState || !bidState.highestBidTeamId) return
    // Play the seconds equal to currentBid
    const seconds = bidState.currentBid
    // For YouTube we approximate by setting end = start + seconds
    const s = songs[currentIndex]
    if(!s) return
    // override player to play snippet of length seconds
    if(playerRef.current){
      const start = s.startSec || 0
      playerRef.current.seekTo(start)
      playerRef.current.playFor(seconds)
    }
    // Lock buzzer for the bidding team until they answer
    setBuzzLocked(true)
    setCurrentBuzzer(bidState.highestBidTeamId)
    // clear bid state
    setBidState(null)
    beep(1100, 0.12)
  }

  // Phase 3: Golden Medley
  function startGoldenMedley(){
    // Leading team
    const leader = [...teams].sort((a,b)=>b.score-a.score)[0]
    setGoldenState({ teamId: leader.id, secondsLeft: 30, currentSongIdx: 0, correctCount: 0, expectedMax: 7 })
    setPhase(3)
    beep(1000, 0.12)
  }

  function goldenAdvanceSong(calledCorrect=false){
    if(!goldenState) return
    const nextIdx = goldenState.currentSongIdx + 1
    const newCorrect = calledCorrect ? goldenState.correctCount + 1 : goldenState.correctCount
    setGoldenState(gs => ({ ...gs, currentSongIdx: nextIdx, correctCount: newCorrect }))
    setCurrentIndex(nextIdx)
    if(calledCorrect) beep(1200, 0.12)
    else beep(300, 0.12)
  }

  // Simple fuzzy check
  function normalize(s=''){ return s.toLowerCase().replace(/[^a-z0-9]+/g,'').trim() }
  function levenshtein(a,b){
    if(a===b) return 0
    const m = a.length, n = b.length
    const dp = Array.from({length:m+1},()=>Array(n+1).fill(0))
    for(let i=0;i<=m;i++) dp[i][0]=i
    for(let j=0;j<=n;j++) dp[0][j]=j
    for(let i=1;i<=m;i++){
      for(let j=1;j<=n;j++){
        const cost = a[i-1]===b[j-1]?0:1
        dp[i][j] = Math.min(dp[i-1][j]+1, dp[i][j-1]+1, dp[i-1][j-1]+cost)
      }
    }
    return dp[m][n]
  }

  function checkAnswer(title, artist, song, strict){
    const t = normalize(title||'')
    const a = normalize(artist||'')
    const st = normalize(song.title||'')
    const sa = normalize(song.artist||'')
    if(strict){
      const lt = levenshtein(t, st)
      const la = levenshtein(a, sa)
      return lt <= 2 && la <= 3
    } else {
      const lt = levenshtein(t, st)
      return lt <= 3
    }
  }

  // persistence
  async function saveSession(){
    const payload = { teams, songs, currentIndex, phase }
    await fetch('/api/save-session', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) })
    alert('Session saved')
  }
  async function loadSession(){
    const r = await fetch('/api/load-session')
    if(r.ok){
      const data = await r.json()
      if(data.teams) setTeams(data.teams)
      if(data.songs) setSongs(data.songs)
      if(typeof data.currentIndex === 'number') setCurrentIndex(data.currentIndex)
      if(typeof data.phase === 'number') setPhase(data.phase)
      alert('Session loaded')
    } else alert('No saved session')
  }

  const currentSong = songs[currentIndex]

  return (
    <div style={{padding:28,fontFamily:'sans-serif'}}>
      <h1 style={{fontSize:42}}>TunedUp — Host Screen (MVP)</h1>
      <div style={{display:'flex',gap:24}}>
        <div style={{flex:1}}>
          <h2 style={{fontSize:28}}>Scoreboard</h2>
          <div style={{display:'flex',gap:16,flexWrap:'wrap'}}>
            {teams.map(t=> (
              <div key={t.id} style={{border:'1px solid #ccc',padding:16,minWidth:240,background: currentBuzzer===t.id? '#fffae6':'white',borderRadius:10}}>
                <div style={{display:'flex',justifyContent:'space-between',alignItems:'center'}}>
                  <input value={t.name} onChange={e=>updateTeamName(t.id,e.target.value)} style={{fontSize:20,fontWeight:'bold',border:0,background:'transparent'}} />
                  <div style={{fontSize:36,fontWeight:'700'}}>{t.score}</div>
                </div>
                <div style={{marginTop:12,display:'flex',gap:12}}>
                  <button onClick={()=>onBuzz(t.id)} style={{padding:'18px 24px',fontSize:20,borderRadius:8,flex:1}}>Buzz</button>
                  <button onClick={()=>{ setTeams(ts=>ts.map(x=>x.id===t.id?{...x,score:x.score+5}:x))}} style={{padding:'10px 12px'}}>+5</button>
                  <button onClick={()=>{ setTeams(ts=>ts.map(x=>x.id===t.id?{...x,score:x.score-5}:x))}} style={{padding:'10px 12px'}}>-5</button>
                </div>
              </div>
            ))}
          </div>

          <div style={{marginTop:20}}>
            <div style={{display:'flex',gap:10,alignItems:'center'}}>
              <button onClick={()=>setPhase(1)} style={{padding:'10px 14px'}}>Phase 1: Standard</button>
              <button onClick={startBidding} style={{padding:'10px 14px'}}>Phase 2: Bid-A-Note (start)</button>
              <button onClick={startGoldenMedley} style={{padding:'10px 14px'}}>Phase 3: Golden Medley</button>
              <label style={{marginLeft:12,fontSize:16}}><input type="checkbox" checked={strictMode} onChange={e=>setStrictMode(e.target.checked)} /> Strict (title+artist)</label>
            </div>

            <div style={{marginTop:14}}>
              <button onClick={()=>acceptAnswerQuick(determinePointsForCurrent())} style={{padding:'10px 14px'}}>Accept Correct (+pts)</button>
              <button onClick={()=>rejectAnswerQuick(determinePointsForCurrent())} style={{marginLeft:8,padding:'10px 14px'}}>Reject / Wrong (-pts)</button>
              <button onClick={nextSong} style={{marginLeft:8,padding:'10px 14px'}}>Next Song</button>
              <button onClick={saveSession} style={{marginLeft:8,padding:'10px 14px'}}>Save Session</button>
              <button onClick={loadSession} style={{marginLeft:8,padding:'10px 14px'}}>Load Session</button>
            </div>
          </div>

          {bidState && (
            <div style={{marginTop:18}}>
              <BidPanel bidState={bidState} teams={teams} onTeamBid={teamPlaceBid} onEndBidding={endBiddingAndPlay} />
            </div>
          )}

        </div>
        <div style={{flex:1}}>
          <h2 style={{fontSize:28}}>Player</h2>
          {currentSong ? (
            <div>
              <div style={{marginBottom:12,fontSize:20}}><strong>{currentSong.title}</strong> — {currentSong.artist}</div>
              <YouTubePlayer ref={playerRef} videoId={currentSong.youtubeId} start={currentSong.startSec} end={currentSong.endSec} />
            </div>
          ) : (
            <div style={{fontSize:18}}>No song loaded</div>
          )}

          {phase===3 && goldenState && (
            <div style={{marginTop:18}}>
              <h3 style={{fontSize:20}}>Golden Medley — Team: {teams.find(t=>t.id===goldenState.teamId)?.name}</h3>
              <GoldenMedleyTimer state={goldenState} onTick={(s)=>setGoldenState(s)} onAdvance={(correct)=>goldenAdvanceSong(correct)} />
            </div>
          )}
        </div>
      </div>

      {answerModalOpen && (
        <AnswerModal onClose={()=>{ setAnswerModalOpen(false); setCurrentBuzzer(null); setBuzzLocked(false); if(playerRef.current) playerRef.current.play() }} onSubmit={(ans,correct)=>handleAnswerSubmit(ans,correct)} strictMode={strictMode} />
      )}
    </div>
  )
}
