# Chowla cosine construction: audited research baseline

This note is a starting point, not a solution. The target is an open problem as
of 2026-08-08. Do not turn a heuristic, a multiset construction, or a sampled
numerical check into a theorem.

## Normalization and status

For a finite set of distinct positive integers (A), write

\[
  f_A(x)=\sum_{a\in A}\cos(ax),\qquad
  \mu(A)=-\min_x f_A(x),\qquad
  K(N)=\inf_{|A|=N}\mu(A).
\]

The requested algorithm would imply

\[
  \liminf_{N\to\infty}\frac{K(N)}{\sqrt N}=0.
\]

It need not imply the corresponding limit for every (N), because the output
cardinality may exceed the input lower bound. It would nevertheless refute the
uniform square-root lower bound conjectured by Chowla.

The latest primary lower bound located in the literature is Bedert's
(K(N)\ge N^{1/5-o(1)}), revised 2026-07-24:

- https://arxiv.org/abs/2509.05260
- https://arxiv.org/html/2509.05260v3

An independent spectral/Cayley-graph route gives (N^{1/10-o(1)}):

- https://arxiv.org/abs/2509.03490
- https://arxiv.org/html/2509.03490v2

The benchmark page remains marked unsolved and reports no known example with
normalized constant at most (1/20):

- https://epoch.ai/frontiermath/open-problems/chowla-cosine

## Exact Sidon-difference identity

Let (B=\{b_1,\dots,b_m\}\) have pairwise-distinct positive differences and
let

\[
  D_+(B)=\{b_j-b_i:1\le i<j\le m\}.
\]

Then (D_+(B)) is a genuine set of (m(m-1)/2) positive integers and, for
(P_B(z)=\sum_{b\in B}z^b),

\[
  2f_{D_+(B)}(x)=|P_B(e^{ix})|^2-m.
\]

Consequently, if

\[
  \delta(B)=\min_{|z|=1}|P_B(z)|,
\]

then the exact normalized constant is

\[
  \frac{\mu(D_+(B))}{\sqrt{|D_+(B)|}}
  =\frac{m-\delta(B)^2}{\sqrt{2m(m-1)}}.
\]

Ordinary Sidon sets use only (delta(B)^2\ge0) and approach (1/\sqrt2).
Within this family, an arbitrary-small target requires the much stronger
(delta(B)^2/m\to1): a Sidon-constrained, near-ultraflat Newman family. A
Newman polynomial with merely (delta(B)\ge m^\alpha), (alpha<1/2), does
not improve the asymptotic constant. Mercer's source for the standard
construction and Newman products is:

- https://arxiv.org/abs/1709.06612

## Mechanically checked barriers

1. **Dilation does nothing.** For a positive integer (q),
   (f_{qA}(x)=f_A(qx)), so the value range and normalized constant are
   unchanged.

   Frequency translation is emphatically not an invariant. If
   (A+t=\{a+t:a\in A\}), then
   (P_{A+t}(e^{ix})=e^{itx}P_A(e^{ix})). At (x=\pi/t), the phase is
   (-1) while (P_A(e^{i\pi/t})\to |A|); hence a large offset creates an
   increasingly narrow trough near (-|A|). This rules out the tempting
   strategy of tagging repeated frequencies with one common large offset and
   also explains why coarse numerical sampling can miss a fatal valley.

2. **Separated unions do not automatically improve the constant.** The safe
   lower bound for a disjoint union is additive in the two negative minima.
   With a very large scale separation, simultaneous Diophantine approximation
   makes this bound nearly sharp: a negative trough of the fast block can be
   placed arbitrarily close to a trough of the slow block.

3. **Cosine products create a positive-peak/negative-trough obstruction.** A
   no-collision identity of the form
   (f_E(x)=2f_A(x)f_B(Mx)) also permits the large positive value
   (f_A(0)=|A|) to multiply a negative trough of the fast factor. It does not
   preserve a small one-sided negative minimum.

4. **Newman digit products do not solve the Sidon-flatness problem.** They
   multiply the relative squared lower-modulus parameter
   (delta(B)^2/|B|). Repeating a fixed seed whose parameter is strictly below
   one drives the parameter down, not up. Cartesian digit products also
   introduce repeated differences unless one factor is trivial.

   There is also an exact coefficient-one strong-product identity. If (M)
   is chosen so that all displayed frequencies are positive and distinct, set

   \[
     C=A\cup MB\cup\{Mb+a,Mb-a:a\in A,b\in B\}.
   \]

   Then

   \[
     1+2f_C(x)=(1+2f_A(x))(1+2f_B(Mx)).
   \]

   This does not amplify a good one-sided bound. Every nonempty cosine set has
   (mu(A)>1/2): from (-K\le f_A\le |A|), integration of
   ((f_A+K)(|A|-f_A)\ge0) gives
   (int f_A^2=|A|/2\le |A|K), with strictness because a nonconstant
   trigonometric polynomial is not two-valued. Thus both product factors take
   negative values. At large scale separation, the large positive value of one
   factor can be aligned arbitrarily closely with a negative trough of the
   other, making the one-sided minimum worse.

5. **Finite cyclic difference sets are not automatically continuous-circle
   solutions.** Their character values are flat at roots of unity, while the
   polynomial may dip sharply between those points. The target quantifier is
   every real (x), not a finite grid.

6. **Multiset identities are inadmissible.** Any convolution, product, or
   autocorrelation construction must prove that every positive output
   frequency has coefficient exactly one. Collision-free support is part of
   the theorem, not an implementation detail.

   This is not a cosmetic restriction. Belov and Konyagin construct
   nonnegative trigonometric polynomials

   \[
     T(x)=a_0+\sum_{k\ge1}a_k\cos(kx),\qquad
     a_k\in\mathbb Z_{\ge0},
   \]

   with total nonconstant coefficient mass (n=\sum_{k\ge1}a_k) and a very
   small free term (of polylogarithmic order). Interpreting (a_k) as repeated
   frequencies essentially solves the multiset analogue. The open bottleneck
   is a deterministic, continuum-safe **atomization** or (0/1) one-sided
   spectral sparsification that replaces those multiplicities by distinct
   integer frequencies without losing the lower bound. Naive large-scale tags
   do not do this because their negative phases can align. Primary source:

   - https://www.mathnet.ru/eng/im95

## Audited finite numerical evidence

Finite examples can guide conjectures, but they cannot establish the required
asymptotic algorithm. The following values were independently checked by
writing (f_A(x)=\sum_{a\in A}T_a(t)), (t=\cos x\), isolating every real root
of the exact integer derivative on ([-1,1]) with Sturm methods, and bounding
the value on each isolating interval.

- (A=\{1,2,4,6,7,8\}):
  (min f_A\approx-1.591832329323849), normalized constant
  (approx0.6498628272).
- (A=\{1,2,3,4,5,7,8,9,10,12\}):
  (min f_A\approx-2.073942427323005), normalized constant
  (approx0.655838181).
- (A=\{1,2,3,4,5,6,7,8,10,12,13,14,17,18,20\}):
  (min f_A\approx-2.515690565269610), normalized constant
  (approx0.649548511).
- A 28-term locally searched example reached approximately (0.65239), not a
  decreasing trend.

These are finite-box local-search records, not proofs of optimality. Their main
lesson is that the elementary Sidon constant is not a universal finite-(N)
barrier, while no scaling law toward zero is visible.

## Required audit for any proposed solution

Every proposed construction must separately prove all of the following.

1. The function terminates for every representable float (c>0) and every
   positive integer (n).
2. The returned decimal integers are positive, pairwise distinct, and finite in
   number, with actual cardinality at least (n).
3. Any product or digit representation has a unique-frequency proof; hidden
   multiplicities are forbidden.
4. The inequality is global on the continuum (x\in[0,2\pi]), not merely on
   roots of unity or a numerical mesh.
5. Parameter choices imply the stated bound with the actual output cardinality.
6. The Python implementation exactly realizes the mathematical set and does
   not rely on floating-point overflow, NaNs, argument-reduction error, or
   private-verifier behavior.

For numerical exploration, a dense grid may locate candidates, but a rigorous
certificate should use exact derivative-root isolation for moderate maximum
frequency, or interval branch-and-bound with an explicit derivative/curvature
remainder. The elementary bounds

\[
  |f_A'(x)|\le\sum_{a\in A}a,\qquad
  |f_A''(x)|\le\sum_{a\in A}a^2
\]

show why a fixed mesh is unsafe when the largest frequency is large.
