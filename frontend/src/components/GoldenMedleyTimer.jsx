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
    <div style={{border:'1px solid #ddd',padding:12,borderRadius:8}}>
      <div style={{fontSize:24,fontWeight:'bold'}}>Time left: {seconds}s</div>
      <div style={{marginTop:8}}>
        <button onClick={()=>onAdvance(true)}>Correct (advance)</button>
        <button onClick={()=>onAdvance(false)} style={{marginLeft:8}}>Wrong (advance)</button>
      </div>
    </div>
  )
}
