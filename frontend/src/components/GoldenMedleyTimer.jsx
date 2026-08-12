import React, { useEffect, useState } from 'react'

export default function GoldenMedleyTimer({ state, onTick, onAdvance }){
  const [seconds, setSeconds] = useState(state.secondsLeft)

  useEffect(()=>{
    setSeconds(state.secondsLeft)
    const t = setInterval(()=>{
      setSeconds(s => {
        const ns = s-1
        if(ns <= 0){ clearInterval(t); onTick({...state, secondsLeft: 0}) }
        else onTick({...state, secondsLeft: ns})
        return ns
      })
    }, 1000)
    return ()=>clearInterval(t)
  }, [])

  return (
    <div style={{border:'1px solid #ddd',padding:16,borderRadius:8}}>
      <div style={{fontSize:28,fontWeight:'bold'}}>Time left: {seconds}s</div>
      <div style={{marginTop:12}}>
        <button onClick={()=>onAdvance(true)} style={{padding:'10px 14px',fontSize:16}}>Correct (advance)</button>
        <button onClick={()=>onAdvance(false)} style={{marginLeft:8,padding:'10px 14px',fontSize:16}}>Wrong (advance)</button>
      </div>
    </div>
  )
}
