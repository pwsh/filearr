package query

// boundedLevenshtein returns the Levenshtein edit distance between a and b,
// giving up early (returning max+1) once the running minimum in a row exceeds
// max. Bounding keeps the fuzzy re-rank cheap: over a candidate pool we only
// care whether a token is within a small distance, never the exact distance of
// far-apart strings. Case folding is the caller's responsibility.
func boundedLevenshtein(a, b string, max int) int {
	ra, rb := []rune(a), []rune(b)
	la, lb := len(ra), len(rb)
	if abs(la-lb) > max {
		return max + 1
	}
	if la == 0 {
		return min(lb, max+1)
	}
	if lb == 0 {
		return min(la, max+1)
	}
	prev := make([]int, lb+1)
	cur := make([]int, lb+1)
	for j := 0; j <= lb; j++ {
		prev[j] = j
	}
	for i := 1; i <= la; i++ {
		cur[0] = i
		rowMin := cur[0]
		for j := 1; j <= lb; j++ {
			cost := 1
			if ra[i-1] == rb[j-1] {
				cost = 0
			}
			cur[j] = min3(prev[j]+1, cur[j-1]+1, prev[j-1]+cost)
			if cur[j] < rowMin {
				rowMin = cur[j]
			}
		}
		if rowMin > max {
			return max + 1
		}
		prev, cur = cur, prev
	}
	return prev[lb]
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

func min3(a, b, c int) int { return min(min(a, b), c) }
