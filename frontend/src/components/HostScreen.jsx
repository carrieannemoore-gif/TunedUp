import React, { useEffect, useState, useRef } from 'react'
import YouTubePlayer from './YouTubePlayer'

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

  function onBuzz(teamId){
    if(buzzLocked) return
    setCurrentBuzzer(teamId)
    setBuzzLocked(true)
    // pause playback
    if(playerRef.current) playerRef.current.pause()
  }

  function acceptAnswer(correct=true, points=10){
    if(!currentBuzzer) return
    setTeams(ts => ts.map(t => t.id===currentBuzzer ? {...t, score: t.score + (correct? points: -points)} : t))
    setCurrentBuzzer(null)
    setBuzzLocked(false)
    if(playerRef.current) playerRef.current.play()
  }

  function nextSong(){
    setCurrentIndex(i => Math.min(i+1, songs.length-1))
    setCurrentBuzzer(null)
    setBuzzLocked(false)
  }

  const currentSong = songs[currentIndex]

  return (
    <div style={{padding:20,fontFamily:'sans-serif'}}>
      <h1>TunedUp — Host Screen (MVP)</h1>
      <div style={{display:'flex',gap:20}}>
        <div style={{flex:1}}>
          <h2>Scoreboard</h2>
          <div style={{display:'flex',gap:10}}>
            {teams.map(t=> (
              <div key={t.id} style={{border:'1px solid #ccc',padding:10,minWidth:120,background: currentBuzzer===t.id? '#fffae6':'white'}}>
                <div style={{fontSize:18,fontWeight:'bold'}}>{t.name}</div>
                <div style={{fontSize:24}}>{t.score}</div>
                <button onClick={()=>onBuzz(t.id)} style={{marginTop:8,padding:'8px 12px'}}>Buzz</button>
              </div>
            ))}
          </div>
          <div style={{marginTop:20}}>
            <button onClick={()=>acceptAnswer(true,10)}>Accept Correct (+10)</button>
            <button onClick={()=>acceptAnswer(false,10)} style={{marginLeft:8}}>Reject / Wrong (-10)</button>
            <button onClick={nextSong} style={{marginLeft:8}}>Next Song</button>
          </div>
        </div>
        <div style={{flex:1}}>
          <h2>Player</h2>
          {currentSong ? (
            <div>
              <div style={{marginBottom:8}}><strong>{currentSong.title}</strong> — {currentSong.artist}</div>
              <YouTubePlayer ref={playerRef} videoId={currentSong.youtubeId} start={currentSong.startSec} end={currentSong.endSec} />
            </div>
          ) : (
            <div>No song loaded</div>
          )}
        </div>
      </div>
    </div>
  )
}
